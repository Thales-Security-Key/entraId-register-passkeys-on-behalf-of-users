# BSD 2-Clause License
#
# Copyright (c) 2024, Yubico AB
#
#   Redistribution and use in source and binary forms, with or
#   without modification, are permitted provided that the following
#   conditions are met:
#
#    1. Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#    2. Redistributions in binary form must reproduce the above
#       copyright notice, this list of conditions and the following
#       disclaimer in the documentation and/or other materials provided
#       with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

import base64
import csv
import ctypes
import datetime
import json
import re
import sys
from getpass import getpass
import secrets
import string
from ykman.device import list_all_devices
import struct
from time import sleep

import requests
import urllib3
from fido2.client import Fido2Client, UserInteraction, WindowsClient
from fido2.ctap2.extensions import CredProtectExtension
from fido2.hid import CtapHidDevice
from fido2.utils import websafe_decode, websafe_encode
from fido2.ctap2 import Ctap2, Config
from fido2.ctap2.pin import ClientPin
from fido2.pcsc import SW_SUCCESS

# Disabling warnings that get produced when certificate stores aren't updated
# to check certificate validity.
# Not recommended for production code to disable the warnings.
# This is the warning that is produced when the warnings are not disabled.
# InsecureRequestWarning: Unverified HTTPS request is being made
# to host 'login.microsoftonline.com'.
# Adding certificate verification is strongly advised. See:
# https://urllib3.readthedocs.io/en/latest/advanced-usage.html#tls-warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
requests.packages.urllib3.disable_warnings()


in_csv_file_name = "./usersToRegister.csv"
out_csv_file_name = "./keysRegistered.csv"
config_file_name = "configs.json"
pin = ""

with open(config_file_name, "r", encoding="utf8") as f:
    configs = json.load(f)


try:
    from fido2.pcsc import CtapPcscDevice
except ImportError:
    CtapPcscDevice = None


def enumerate_devices():
    for dev in CtapHidDevice.list_devices():
        yield dev
    if CtapPcscDevice:
        for dev in CtapPcscDevice.list_devices():
            yield dev

def wait_device():
    print("\n-----")
    print(">>> Waiting for Security Key to be plugged in...")
    return wait_device_loop()

def wait_device_loop():
    devices = list(enumerate_devices())
    if( len(devices) == 0):
        sleep(0.2)
        return wait_device_loop()
    if( len(devices) > 1):
        raise Exception("More than one device found.")
    return devices[0]

# Handle user interaction
class CliInteraction(UserInteraction):    
    def prompt_up(self):
        print("\nTouch your security key now...\n")

    def request_pin(self, permissions, rp_id):
        if not configs["setRandomPIN"]:
            return getpass("Enter PIN: ")            
        else:
            return pin

    def request_uv(self, permissions, rp_id):
        print("User Verification required.")
        return True


def base64url_to_bytearray(b64url_string):
    temp = b64url_string.replace("_", "/").replace("-", "+")
    return bytearray(
        base64.urlsafe_b64decode(temp + "=" * (4 - len(temp) % 4))
    )


def create_credentials_on_security_key(
    user_id, challenge, user_display_name, user_name,rp_id
):    
    print("-----")
    print("in create_credentials_on_security_key\n")
    print(
        "\tPrepare for FIDO2 Registration Ceremony and follow the prompts\n"
    )    
    print("\tPress Enter when security key is ready\n")
    device = wait_device()
    serial_number = get_serial_number(device)

    if (
        WindowsClient.is_available()
        and not ctypes.windll.shell32.IsUserAnAdmin()
    ):
        # Use the Windows WebAuthn API if available, and we're not running        
        client = WindowsClient("https://" + rp_id)

        # Config file setting for setRandomPIN doesn't apply in this scenario
        global pin
        pin = "n/a"
    else:
        generate_and_set_pin(device)

        client = Fido2Client(
                device,
                "https://" + rp_id,
                user_interaction=CliInteraction(),
            )            
        if (client.info.options.get("rk") == False):
            print(
                "No security key with support for discoverable"
                " credentials found"
            )
            sys.exit(1)


    pkcco = build_creation_options(
        challenge, user_id, user_display_name, user_name, rp_id
    )

    result = client.make_credential(pkcco["publicKey"])

    print("\tNew FIDO credential created on YubiKey")

    attestation_obj = result["attestationObject"]
    attestation = websafe_encode(attestation_obj)
    print(f"Attestation: {attestation}")

    client_data = result["clientData"].b64

    credential_id = websafe_encode(
        result.attestation_object.auth_data.credential_data.credential_id
    )
    print(f"\ncredentialId: {credential_id}")

    client_extenstion_results = websafe_encode(
        json.dumps(result.attestation_object.auth_data.extensions).encode(
            "utf-8"
        )
    )
    print(f"\nclientExtensions: {websafe_decode(client_extenstion_results)}")

    # Set min pin length and force pin change flags
    if configs["setMinimumPINLength"] or configs["setForceChangePin"]:
        set_ctap21_flags(device)

    return (
        attestation,
        client_data,
        credential_id,
        client_extenstion_results,
        serial_number,
    )


def set_http_headers(access_token):
    return {
        "Accept": "application/json",
        "Authorization": access_token,
        "Content-Type": "application/json",
        "Accept-Encoding": "gzip, deflate, br",
    }


def build_creation_options(challenge, userId, displayName, name, rp_id):
    # Most of the creation options are static and shouldn't change for each
    # user and for each request so this script staticly defines the creation
    # options that are retrieved from Microsoft Graph. Ideally these would
    # be retrieved directly from Microsoft Graph in case they do change.

    # Note about overriding the value for credentialProtectionPolicy.
    # The fido2 library only supports setting the credProtect extension
    # using the enum not the string value. OPTIONAL is equivalent
    # to "userVerificationOptional" which is also equivalent to "Level 1"

    # Note at the time of writing this, webauthn.dll does not set
    # credprotect extensions. Run in admin mode if credprotect
    # extensions must be set for your scenario and for your
    # fido2 security keys. The default behavior of YubiKeys is to
    # use credprotect level 1 if not explicitly set, the default value
    # aligns with the what Microsoft Graph expects to be used.
    # If credprotect > 1 is used on a security key, you should expect
    # Windows 10 desktop login scenarios to fail.    
    public_key_credential_creation_options = {
        "publicKey": {
            "challenge": base64url_to_bytearray(challenge),
            "timeout": 0,
            "attestation": "direct",
            "rp": {"id": rp_id, "name": "Microsoft"},
            "user": {
                "id": base64url_to_bytearray(userId),
                "displayName": displayName,
                "name": name,
            },
            "pubKeyCredParams": [
                {"type": "public-key", "alg": -7},
                {"type": "public-key", "alg": -257},
            ],
            "excludeCredentials": [],
            "authenticatorSelection": {
                "authenticatorAttachment": "cross-platform",
                "requireResidentKey": True,
                "userVerification": "required",
            },
            "extensions": {
                "hmacCreateSecret": True,
                "enforceCredentialProtectionPolicy": True,
                "credentialProtectionPolicy": CredProtectExtension.POLICY.OPTIONAL,
            },
        }
    }

    return public_key_credential_creation_options


def get_access_token_for_microsoft_graph():
    # Request a token for Graph
    # Use client_credentials grant
    print("-----")
    print("in get_access_token_for_microsoft_graph\n")
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    token_endpoint = (
        "https://login.microsoftonline.com/"
        + configs["tenantName"]
        + "/oauth2/v2.0/token"
    )

    body = {
        "grant_type": "client_credentials",
        "client_id": configs["client_id"],
        "client_secret": configs["client_secret"],
        "scope": "https://graph.microsoft.com/.default",
    }

    token_response = requests.post(
        token_endpoint, data=body, headers=headers, verify=False
    )

    access_token = re.search(
        '"access_token":"([^"]+)"', str(token_response.content)
    )

    decoded_response = json.loads(token_response.content)
    if "error" in decoded_response.keys():
        raise Exception(
            decoded_response["error"], decoded_response["error_description"]
        )

    print("\t retrieved access token using app credentials")
    return access_token.group(1)


# Call the Microsoft Graph to create a fido2method
def create_and_activate_fido_method(
    credential_id,
    client_extensions,
    user_name,
    attestation,
    client_data,
    serial_number,
    access_token,
):
    print("-----")
    print("in create_and_activate_fido_method\n")

    headers = set_http_headers(access_token)

    fido_credentials_endpoint = (
        "https://graph.microsoft.com/beta/users/"
        + user_name
        + "/authentication/fido2Methods"
    )

    body = {
        "publicKeyCredential": {
            "id": credential_id,
            "response": {
                "attestationObject": attestation,
                "clientDataJSON": client_data,
            },
            "clientExtensionResults": json.loads(
                base64.b64decode(str(client_extensions)).decode("utf-8")
            ),
        },
        "displayName": "Serial: "
        + str(serial_number)
        + " "
        + str(datetime.date.today()),
    }

    response = requests.post(
        fido_credentials_endpoint, json=body, headers=headers, verify=False
    )

    if response.status_code == 201:
        create_response = response.json()
        print("\tRegistration success.")
        print(f'\tAuth method objectId: {create_response["id"]}')
        return True, create_response["id"]
    else:
        print(response.status_code)
        print(response.content)
        return False, []


def generate_pin():
    disallowed_pins = [
        "12345678",
        "12341234",
        "87654321",
        "12344321",
        "11223344",
        "12121212",
        "123456",
        "123123",
        "654321",
        "123321",
        "112233",
        "121212",
        "520520",
        "123654",
        "159753",
    ]

    # Get length
    length = configs["randomPINLength"]

    while True:
        digits = "".join(secrets.choice(string.digits) for _ in range(length))
        # Check if PIN is not trivial and not in banned list
        if len(set(digits)) != 1 and digits not in disallowed_pins:
            return digits


def generate_and_set_pin(device):
    print("-----")
    print("in generate_and_set_pin\n")
    global pin
    if configs["setRandomPIN"]:
        ctap = Ctap2(device)
        if ctap.info.options.get("clientPin"):
            print("\tPIN already set for the device. Quitting.")
            print(
                "\tReset YubiKey and rerun the script if you want to use the config 'setRandomPIN'"
            )
            quit()
        pin = generate_pin()
        print(f"\tWe will now set the PIN to: {pin} \n")
        client_pin = ClientPin(ctap)
        client_pin.set_pin(pin)
        print(f"\tPIN set to {pin}")
    else:
        print("\tNot generating PIN. Allowing platform to prompt for PIN\n")


def set_ctap21_flags(device):
    global pin    
    #No need to try if using the Windows client (as non-admin)
    if not (
        WindowsClient.is_available()
        and not ctypes.windll.shell32.IsUserAnAdmin()
    ):
        
        if not configs['setRandomPIN']:
            #Need to prompt for PIN again if using user supplied PIN
            print("PIN required to set minimum length and force pin change flags")
            pin = getpass("Please enter the PIN:")

        ctap = Ctap2(device)

        if ctap.info.options.get("setMinPINLength"):
            client_pin = ClientPin(ctap)
            token = client_pin.get_pin_token(
                pin, ClientPin.PERMISSION.AUTHENTICATOR_CFG
            )
            config = Config(ctap, client_pin.protocol, token)

            # Set PIN length
            if configs["setMinimumPINLength"]:
                length = configs["minimumPINLength"]
                print("\tGoing to set the minimum pin length to " + str(length) + ".")
                config.set_min_pin_length(min_pin_length=length)
            
            # Set Force Change PIN
            if configs["setForceChangePin"]:
                print("\tGoing to force a PIN change on first use.")
                config.set_min_pin_length(force_change_pin=True)
    else:
        print(
            "Using these CTAP21 features are not supported when running in this mode"
        )


def get_serial_number(device):
    # Get serial number for YubiKey
    for device, info in list_all_devices():
        print(f"\tFound YubiKey with serial number: {info.serial}")
        return info.serial
    # Get serial number for Thales Security Key
    return get_thales_serial_number(device)


def get_thales_serial_number(device) -> string:
        
    if isinstance(device, CtapHidDevice):
        # Get Thales Serial Number in USB mode
        packet = struct.pack(">IBBBB", device._channel_id, 128 | 0x50, 0x00, 0x01, 0x55)
        device._connection.write_packet(packet.ljust(device._packet_size, b"\0"))        
        recv = device._connection.read_packet()

        r_channel = struct.unpack_from(">I", recv)[0]
        if r_channel != device._channel_id:
            raise Exception("Wrong channel")
        
        if (recv[7] != 0) or (recv[8] != 0x02): 
            raise Exception("Unable to get Thales Serial Number")    
        
        if sys.getsizeof(recv) < 17:
            raise Exception("Unable to get Thales Serial Number")
        
        serial = recv[9:17].decode("utf-8")
    
    else:
        # Get Thales Serial Number in NFC mode
        try:
            AID_CM = b"\xa0\x00\x00\x00\x03\x00\x00\x00"
            apdu = b"\x00\xa4\x04\x00" + struct.pack("!B", len(AID_CM)) + AID_CM
            resp, sw1, sw2 = device.apdu_exchange(apdu)
            if (sw1, sw2) != SW_SUCCESS:
                raise Exception("Card Manager applet selection failure.")

            resp, sw1, sw2 = device.apdu_exchange(b"\x80\xCA\x01\x04")
            if (sw1, sw2) != SW_SUCCESS:
                raise Exception("Unable to get Thales serial number.")
            serial = resp[3:].decode("utf-8")
        except:
            print(f"\tUnable to get serial number for this device.")
            return "" 
        finally:
            device._select()

    print(f"\tFound Thales Security Key with serial number: {serial}")
    return serial


def warn_user_about_pin_behaviors():
    # See BulkRegistration.md for more details
    # Windows configurations to look out for:
    if WindowsClient.is_available():
        # Running on Windows as admin
        if ctypes.windll.shell32.IsUserAnAdmin():
            if not configs["setRandomPIN"]:
                print(
                    "\n\n\tIf PIN is not already set on security key(s), "
                    "then make sure PIN is set on security keys before "
                    "proceeding"
                )
                input("\n\tPress Enter key to continue...")
            if configs["setRandomPIN"]:
                print(
                    "\n\n\tIf PIN is already set on security key(s) then "
                    "script will prompt for existing PIN and change to new "
                    "random PIN."
                )
                input("\n\tPress Enter key to continue...")
        if not ctypes.windll.shell32.IsUserAnAdmin():
            if configs["setRandomPIN"]:
                print(
                    "\n\n\tsetRandomPIN setting is set to true. This "
                    "setting will be ignored. User will be prompted to "
                    "set PIN if it is not already set."
                )
                input("\n\tPress Enter key to continue...")
    # macOS and other platforms configurations to look out for:
    if not WindowsClient.is_available():
        if not configs["setRandomPIN"]:
            print(
                "\n\n\tIf PIN is not already set on security key(s), "
                "then make sure PIN is set on security keys before "
                "proceeding"
            )
            input("\n\tPress Enter key to continue...")
        if configs["setRandomPIN"]:
            print(
                "\n\n\tIf PIN is already set on security key(s) then "
                "script will prompt for existing PIN and change to new "
                "random PIN."
            )
            input("\n\tPress Enter key to continue...")


def main():
    warn_user_about_pin_behaviors()
    access_token = get_access_token_for_microsoft_graph()
    line_count = 0
    with open(in_csv_file_name, newline="") as in_csv_file:
        with open(out_csv_file_name, "w", newline="") as out_csv_file:
            csv_reader = csv.reader(in_csv_file)
            csv_writer = csv.writer(out_csv_file)
            # Write header row for output file registeredKeys.csv
            csv_writer.writerow(
                ["#upn", "entraIDAuthMethodObjectId", "serialNumber", "PIN"]
            )
            for row in csv_reader:
                if line_count == 0:
                    # Assume header exists in the csv and skip this row
                    print("\tSkip csv header row")
                else:
                    user_name = row[0]
                    user_display_name = row[1]
                    user_id = row[2]
                    challenge = row[3]
                    challenge_expiry_time = row[4]
                    rp_id = row[5]
                    print("-------------------------------------------------")
                    print(f"\tprocessing user: {user_name}")
                    print("-------------------------------------------------")
                    print(f"\tuserDisplayName: {user_display_name}")
                    print(f"\tuserId: {user_id}")
                    print(f"\tchallengeExpiryTime: {challenge_expiry_time}")
                    print(f"\trpID: {rp_id}")
                    print("\n")
                    try:
                        (
                            att,
                            clientData,
                            credId,
                            extn,
                            serial,
                        ) = create_credentials_on_security_key(
                            user_id, challenge, user_display_name, user_name,rp_id
                        )
                        activated, auth_method = create_and_activate_fido_method(
                            credId,
                            extn,
                            user_name,
                            att,
                            clientData,
                            serial,
                            access_token,
                        )
                    except Exception as error:
                        print("\n\tERROR >> " + str(error))
                        print("\tERROR >> Exiting\n")
                        return

                    print(
                        "\n\tCompleted registration and configuration "
                        + f"for user: {user_name}"
                    )

                    # Write CSV with security key registration details
                    # username,authMethodID,serialNumber,PIN
                    csv_writer.writerow([user_name, auth_method, serial, pin])
                    input("\tPress Enter key to continue...")
                    print("-----")

                line_count += 1
    print(
        "\nAfter verifying results, cleanup any csv files"
        + " that are no longer needed.\n"
    )


main()
