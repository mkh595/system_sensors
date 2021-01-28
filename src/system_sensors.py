#!/usr/bin/env python3
import argparse
import datetime as dt
import signal
import sys
import os
import socket
import platform
import threading
import time
from datetime import timedelta
import re
from subprocess import check_output
from rpi_bad_power import new_under_voltage
import paho.mqtt.client as mqtt
import psutil
import pytz
import yaml
import csv
from pytz import timezone

try:
    import apt
    apt_disabled = False
except ImportError:
    apt_disabled = True
UTC = pytz.utc
DEFAULT_TIME_ZONE = None

isDockerized = bool(os.getenv('YES_YOU_ARE_IN_A_CONTAINER', False))

vcgencmd   = "vcgencmd"
os_release = "/etc/os-release"
if isDockerized:
    os_release = "/app/host/os-release"
    vcgencmd   = "/opt/vc/bin/vcgencmd"

# Get OS information
OS_DATA = {}
with open(os_release) as f:
    reader = csv.reader(f, delimiter="=")
    for row in reader:
        if row:
            OS_DATA[row[0]] = row[1]

mqttClient = None
WAIT_TIME_SECONDS = 60
deviceName = None
_underVoltage = None

class ProgramKilled(Exception):
    pass


def signal_handler(signum, frame):
    raise ProgramKilled


class Job(threading.Thread):
    def __init__(self, interval, execute, *args, **kwargs):
        threading.Thread.__init__(self)
        self.daemon = False
        self.stopped = threading.Event()
        self.interval = interval
        self.execute = execute
        self.args = args
        self.kwargs = kwargs

    def stop(self):
        self.stopped.set()
        self.join()

    def run(self):
        while not self.stopped.wait(self.interval.total_seconds()):
            self.execute(*self.args, **self.kwargs)


def write_message_to_console(message):
    print(message)
    sys.stdout.flush()
    

def utc_from_timestamp(timestamp: float) -> dt.datetime:
    """Return a UTC time from a timestamp."""
    return UTC.localize(dt.datetime.utcfromtimestamp(timestamp))


def as_local(dattim: dt.datetime) -> dt.datetime:
    """Convert a UTC datetime object to local time zone."""
    if dattim.tzinfo == DEFAULT_TIME_ZONE:
        return dattim
    if dattim.tzinfo is None:
        dattim = UTC.localize(dattim)

    return dattim.astimezone(DEFAULT_TIME_ZONE)

def get_last_boot():
    return str(as_local(utc_from_timestamp(psutil.boot_time())).isoformat())

def get_last_message():
    return str(as_local(utc_from_timestamp(time.time())).isoformat())


def on_message(client, userdata, message):
    print (f"Message received: {message.payload.decode()}"  )

    if message.payload.decode() == "online":
        send_config_message(client)
    elif message.payload.decode() == "display_on":
        reading = check_output([vcgencmd, "display_power", "1"]).decode("UTF-8")
        updateSensors()
    elif message.payload.decode() == "display_off":
        reading = check_output([vcgencmd, "display_power", "0"]).decode("UTF-8")
        updateSensors()


def updateSensors():
    payload_str = (
        '{'
        + f'"temperature": {get_temp()},'
        + f'"display": "{get_display_status()}",'
        + f'"disk_use": {get_disk_usage("/")},'
        + f'"memory_use": {get_memory_usage()},'
        + f'"cpu_usage": {get_cpu_usage()},'
        + f'"swap_usage": {get_swap_usage()},'
        + f'"power_status": "{get_rpi_power_status()}",'
        + f'"last_boot": "{get_last_boot()}",'
        + f'"last_message": "{get_last_message()}",'
        + f'"host_name": "{get_host_name()}",'
        + f'"host_ip": "{get_host_ip()}",'
        + f'"host_os": "{get_host_os()}",'
        + f'"host_arch": "{get_host_arch()}"'
    )
    if "check_available_updates" in settings and settings["check_available_updates"] and not apt_disabled:
        payload_str = payload_str + f', "updates": {get_updates()}' 
    if "check_wifi_strength" in settings and settings["check_wifi_strength"]:
        payload_str = payload_str + f', "wifi_strength": {get_wifi_strength()}'
    if "external_drives" in settings:
        for drive in settings["external_drives"]:
            payload_str = (
                payload_str + f', "disk_use_{drive.lower()}": {get_disk_usage(settings["external_drives"][drive])}'
            )
    payload_str = payload_str + "}"
    mqttClient.publish(
        topic=f"system-sensors/sensor/{deviceName}/state",
        payload=payload_str,
        qos=1,
        retain=False,
    )


def get_updates():
    cache = apt.Cache()
    cache.open(None)
    cache.upgrade()
    return str(cache.get_changes().__len__())


# Temperature method depending on system distro
def get_temp():
    temp = ""
    if "rasp" in OS_DATA["ID"]:
        reading = check_output([vcgencmd, "measure_temp"]).decode("UTF-8")
        temp = str(re.findall("\d+\.\d+", reading)[0])
    else:
        reading = check_output(["cat", "/sys/class/thermal/thermal_zone0/temp"]).decode("UTF-8")
        temp = str(reading[0] + reading[1] + "." + reading[2])
    return temp

# display power method depending on system distro
def get_display_status():
    display_state = ""
    if "rasp" in OS_DATA["ID"]:
        reading = check_output([vcgencmd, "display_power"]).decode("UTF-8")
        display_state = str(re.findall("^display_power=(?P<display_state>[01]{1})$", reading)[0])
    else:
        display_state = "Unknown"
    return display_state

# host model method depending on system distro
def get_host_model():
    model = ""
    if "rasp" in OS_DATA["ID"] and isDockerized:
        model = check_output(["cat", "/app/host/proc/device-tree/model"]).decode("UTF-8").strip()
        # remove a weird character breaking the json in mqtt explorer
        model = model[:-1]
    else:
        model = "RPI "+deviceNameDisplay
    return model

def get_disk_usage(path):
    return str(psutil.disk_usage(path).percent)

def get_memory_usage():
    return str(psutil.virtual_memory().percent)

def get_cpu_usage():
    return str(psutil.cpu_percent(interval=None))

def get_swap_usage():
    return str(psutil.swap_memory().percent)

def get_wifi_strength():  # check_output(["/proc/net/wireless", "grep wlan0"])
    wifi_strength_value = check_output(
                              [
                                  "bash",
                                  "-c",
                                  "cat /proc/net/wireless | grep wlan0: | awk '{print int($4)}'",
                              ]
                          ).decode("utf-8").rstrip()
    if not wifi_strength_value:
        wifi_strength_value = "0"
    return (wifi_strength_value)

def get_rpi_power_status():
    return _underVoltage.get()

def get_host_name():
    if isDockerized == True:
        # todo add a check to validate the file actually exists, in case someone forgot to map it
        host = check_output(["cat", "/app/host/hostname"]).decode("UTF-8").strip()
    else:
        host = socket.gethostname()
    return host

def get_host_ip():
    if isDockerized == True:
        return get_container_host_ip()
    else:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect(('8.8.8.8', 80))
            return sock.getsockname()[0]
        except socket.error:
            try:
                return socket.gethostbyname(socket.gethostname())
            except socket.gaierror:
                return '127.0.0.1'
        finally:
            sock.close()

def get_container_host_ip():
    # todo add a check to validate the file actually exists, in case someone forgot to map it
     data = check_output(["cat", "/app/host/system_sensor_pipe"]).decode("UTF-8")
     ip = ""
     for line in data.split('\n'):
         mo = re.match ("^.{2}(?P<id>.{2}).{2}(?P<addr>.{8})..{4} .{8}..{4} (?P<status>.{2}).*|", line)
         if mo and mo.group("id") != "sl":
             status = int(mo.group("status"), 16)
             if status == 1: # connection established
                 ip = hex2addr(mo.group("addr"))
                 break
     return ip

def hex2addr(hex_addr):
    l = len(hex_addr)
    first = True
    ip = ""
    for i in range(l // 2):
        if (first != True):
            ip = "%s." % ip
        else:
            first = False
        ip = ip + ("%d" % int(hex_addr[-2:], 16))
        hex_addr = hex_addr[:-2]
    return ip

def get_host_os():
    try:     
        return OS_DATA["PRETTY_NAME"]
    except:
        return "Unknown"

def get_host_arch():    
    try:     
        return platform.machine()
    except:
        return "Unknown"

def remove_old_topics():
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceNameDisplay}/{deviceNameDisplay}Temp/config",
        payload='',
        qos=1,
        retain=False,
    )
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceNameDisplay}/{deviceNameDisplay}DiskUse/config",
        payload='',
        qos=1,
        retain=False,
    )
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceNameDisplay}/{deviceNameDisplay}MemoryUse/config",
        payload='',
        qos=1,
        retain=False,
    )
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceNameDisplay}/{deviceNameDisplay}CpuUsage/config",
        payload='',
        qos=1,
        retain=False,
    )
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceNameDisplay}/{deviceNameDisplay}SwapUsage/config",
        payload='',
        qos=1,
        retain=False,
    )
    mqttClient.publish(
        topic=f"homeassistant/binary_sensor/{deviceNameDisplay}/{deviceNameDisplay}PowerStatus/config",
        payload='',
        qos=1,
        retain=False,
    )
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceNameDisplay}/{deviceNameDisplay}PowerStatus/config",
        payload='',
        qos=1,
        retain=False,
    )
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceNameDisplay}/{deviceNameDisplay}LastBoot/config",
        payload='',
        qos=1,
        retain=False,
    )
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceNameDisplay}/{deviceNameDisplay}LastMessage/config",
        payload='',
        qos=1,
        retain=False,
    )
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceNameDisplay}/{deviceNameDisplay}WifiStrength/config",
        payload='',
        qos=1,
        retain=False,
    )
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceNameDisplay}/{deviceNameDisplay}Updates/config",
        payload='',
        qos=1,
        retain=False,
    )

    if "external_drives" in settings:
        for drive in settings["external_drives"]:
            mqttClient.publish(
                topic=f"homeassistant/sensor/{deviceNameDisplay}/{deviceNameDisplay}DiskUse{drive}/config",
                payload='',
                qos=1,
                retain=False,
            )

    if "rasp" in OS_DATA["ID"]:
        mqttClient.publish(
            topic=f"homeassistant/switch/{deviceNameDisplay}/{deviceNameDisplay}Display/config",

            payload='',
            qos=1,
            retain=False,
        )

def check_settings(settings):
    if "mqtt" not in settings:
        write_message_to_console("Mqtt not defined in settings.yaml! Please check the documentation")
        sys.exit()
    if "hostname" not in settings["mqtt"]:
        write_message_to_console("Hostname not defined in settings.yaml! Please check the documentation")
        sys.exit()
    if "timezone" not in settings:
        write_message_to_console("Timezone not defined in settings.yaml! Please check the documentation")
        sys.exit()
    if "deviceName" not in settings:
        write_message_to_console("deviceName not defined in settings.yaml! Please check the documentation")
        sys.exit()
    if "client_id" not in settings:
        write_message_to_console("client_id not defined in settings.yaml! Please check the documentation")
        sys.exit()
    if "power_integer_state" in settings:
        write_message_to_console("power_integer_state is deprecated please remove this option power state is now a binary_sensor!")

def send_config_message(mqttClient):
    write_message_to_console("send config message")
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceName}/temperature/config",
        payload='{"device_class":"temperature",'
                + f"\"name\":\"{deviceNameDisplay} Temperature\","
                + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                + '"unit_of_measurement":"°C",'
                + '"value_template":"{{value_json.temperature}}",'
                + f"\"unique_id\":\"{deviceName}_sensor_temperature\","
                + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                + f"\"name\":\"{deviceNameDisplay} Sensors\",\"model\":\"{deviceModel}\", \"manufacturer\":\"RPI Foundation\"}},"
                + f"\"icon\":\"mdi:thermometer\"}}",
        qos=1,
        retain=True,
    )
    if "rasp" in OS_DATA["ID"]:
        mqttClient.publish(
            topic=f"homeassistant/switch/{deviceName}/display/config",
            payload='{'
                    + f"\"name\":\"{deviceNameDisplay} Display Switch\","
                    + f"\"unique_id\":\"{deviceName}_switch_display\","
                    + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                    + f"\"command_topic\":\"system-sensors/sensor/{deviceName}/command\","
                    + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                    + '"value_template":"{{value_json.display}}",'
                    + '"state_off":"0",'
                    + '"state_on":"1",'
                    + '"payload_off":"display_off",'
                    + '"payload_on":"display_on",'
                    + f"\"device\":{{"
                        + f"\"identifiers\":[\"{deviceName}_sensor\"],"
                        + f"\"name\":\"{deviceNameDisplay} Sensors\","
                        + f"\"model\":\"{deviceModel}\","
                        + '"manufacturer":"RPI Foundation"'
                    + '},'
                    + '"icon":"mdi:monitor"}',
            qos=1,
            retain=True,
        )

    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceName}/disk_use/config",
        payload=f"{{\"name\":\"{deviceNameDisplay} Disk Use\","
                + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                + '"unit_of_measurement":"%",'
                + '"value_template":"{{value_json.disk_use}}",'
                + f"\"unique_id\":\"{deviceName}_sensor_disk_use\","
                + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                + f"\"name\":\"{deviceNameDisplay} Sensors\",\"model\":\"{deviceModel}\", \"manufacturer\":\"RPI Foundation\"}},"
                + f"\"icon\":\"mdi:micro-sd\"}}",
        qos=1,
        retain=True,
    )
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceName}/memory_use/config",
        payload=f"{{\"name\":\"{deviceNameDisplay} Memory Use\","
                + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                + '"unit_of_measurement":"%",'
                + '"value_template":"{{value_json.memory_use}}",'
                + f"\"unique_id\":\"{deviceName}_sensor_memory_use\","
                + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                + f"\"name\":\"{deviceNameDisplay} Sensors\",\"model\":\"{deviceModel}\", \"manufacturer\":\"RPI Foundation\"}},"
                + f"\"icon\":\"mdi:memory\"}}",
        qos=1,
        retain=True,
    )
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceName}/cpu_usage/config",
        payload=f"{{\"name\":\"{deviceNameDisplay} Cpu Usage\","
                + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                + '"unit_of_measurement":"%",'
                + '"value_template":"{{value_json.cpu_usage}}",'
                + f"\"unique_id\":\"{deviceName}_sensor_cpu_usage\","
                + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                + f"\"name\":\"{deviceNameDisplay} Sensors\",\"model\":\"{deviceModel}\", \"manufacturer\":\"RPI Foundation\"}},"
                + f"\"icon\":\"mdi:memory\"}}",
        qos=1,
        retain=True,
    )
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceName}/swap_usage/config",
        payload=f"{{\"name\":\"{deviceNameDisplay} Swap Usage\","
                + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                + '"unit_of_measurement":"%",'
                + '"value_template":"{{value_json.swap_usage}}",'
                + f"\"unique_id\":\"{deviceName}_sensor_swap_usage\","
                + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                + f"\"name\":\"{deviceNameDisplay} Sensors\",\"model\":\"{deviceModel}\", \"manufacturer\":\"RPI Foundation\"}},"
                + f"\"icon\":\"mdi:harddisk\"}}",
        qos=1,
        retain=True,
    )
    mqttClient.publish(
        topic=f"homeassistant/binary_sensor/{deviceName}/power_status/config",
        payload='{"device_class":"problem",'
                + f"\"name\":\"{deviceNameDisplay} Under Voltage\","
                + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                + '"value_template":"{{value_json.power_status}}",'
                + f"\"unique_id\":\"{deviceName}_sensor_power_status\","
                + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                + f"\"name\":\"{deviceNameDisplay} Sensors\",\"model\":\"{deviceModel}\", \"manufacturer\":\"RPI Foundation\"}}"
                + f"}}",
        qos=1,
        retain=True,
    )
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceName}/last_boot/config",
        payload='{"device_class":"timestamp",'
                + f"\"name\":\"{deviceNameDisplay} Last Boot\","
                + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                + '"value_template":"{{value_json.last_boot}}",'
                + f"\"unique_id\":\"{deviceName}_sensor_last_boot\","
                + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                + f"\"name\":\"{deviceNameDisplay} Sensors\",\"model\":\"{deviceModel}\", \"manufacturer\":\"RPI Foundation\"}},"
                + f"\"icon\":\"mdi:clock\"}}",
        qos=1,
        retain=True,
    )
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceName}/hostname/config",
        payload=f"{{\"name\":\"{deviceNameDisplay} Hostname\","
                + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                + '"value_template":"{{value_json.host_name}}",'
                + f"\"unique_id\":\"{deviceName}_sensor_host_name\","
                + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                + f"\"name\":\"{deviceNameDisplay} Sensors\",\"model\":\"{deviceModel}\", \"manufacturer\":\"RPI Foundation\"}},"
                + f"\"icon\":\"mdi:card-account-details\"}}",
        qos=1,
        retain=True,
    )
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceName}/host_ip/config",
        payload=f"{{\"name\":\"{deviceNameDisplay} Host Ip\","
                + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                + '"value_template":"{{value_json.host_ip}}",'
                + f"\"unique_id\":\"{deviceName}_sensor_host_ip\","
                + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                + f"\"name\":\"{deviceNameDisplay} Sensors\",\"model\":\"{deviceModel}\", \"manufacturer\":\"RPI Foundation\"}},"
                + f"\"icon\":\"mdi:lan\"}}",
        qos=1,
        retain=True,
    )
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceName}/host_os/config",
        payload=f"{{\"name\":\"{deviceNameDisplay} Host OS\","
                + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                + '"value_template":"{{value_json.host_os}}",'
                + f"\"unique_id\":\"{deviceName}_sensor_host_os\","
                + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                + f"\"name\":\"{deviceNameDisplay} Sensors\",\"model\":\"{deviceModel}\", \"manufacturer\":\"RPI Foundation\"}},"
                + f"\"icon\":\"mdi:linux\"}}",
        qos=1,
        retain=True,
    )
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceName}/host_arch/config",
        payload=f"{{\"name\":\"{deviceNameDisplay} Host Architecture\","
                + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                + '"value_template":"{{value_json.host_arch}}",'
                + f"\"unique_id\":\"{deviceName}_sensor_host_arch\","
                + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                + f"\"name\":\"{deviceNameDisplay} Sensors\",\"model\":\"{deviceModel}\", \"manufacturer\":\"RPI Foundation\"}},"
                + f"\"icon\":\"mdi:chip\"}}",
        qos=1,
        retain=True,
    )
    mqttClient.publish(
        topic=f"homeassistant/sensor/{deviceName}/last_message/config",
        payload='{"device_class":"timestamp",'
                + f"\"name\":\"{deviceNameDisplay} Last Message\","
                + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                + '"value_template":"{{value_json.last_message}}",'
                + f"\"unique_id\":\"{deviceName}_sensor_last_message\","
                + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                + f"\"name\":\"{deviceNameDisplay} Sensors\",\"model\":\"{deviceModel}\", \"manufacturer\":\"RPI Foundation\"}},"
                + f"\"icon\":\"mdi:clock-check\"}}",
        qos=1,
        retain=True,
    )

    if "check_available_updates" in settings and settings["check_available_updates"]:
        # import apt
        if(apt_disabled):
            write_message_to_console("import of apt failed!")
        else:
            mqttClient.publish(
                topic=f"homeassistant/sensor/{deviceName}/updates/config",
                payload=f"{{\"name\":\"{deviceNameDisplay} Updates\","
                        + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                        + '"value_template":"{{value_json.updates}}",'
                        + f"\"unique_id\":\"{deviceName}_sensor_updates\","
                        + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                        + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                        + f"\"name\":\"{deviceNameDisplay} Sensors\",\"model\":\"{deviceModel}\", \"manufacturer\":\"RPI Foundation\"}},"
                        + f"\"icon\":\"mdi:cellphone-arrow-down\"}}",
                qos=1,
                retain=True,
            )

    if "check_wifi_strength" in settings and settings["check_wifi_strength"]:
        mqttClient.publish(
            topic=f"homeassistant/sensor/{deviceName}/wifi_strength/config",
            payload='{"device_class":"signal_strength",'
                    + f"\"name\":\"{deviceNameDisplay} Wifi Strength\","
                    + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                    + '"unit_of_measurement":"dBm",'
                    + '"value_template":"{{value_json.wifi_strength}}",'
                    + f"\"unique_id\":\"{deviceName}_sensor_wifi_strength\","
                    + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                    + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                    + f"\"name\":\"{deviceNameDisplay} Sensors\",\"model\":\"{deviceModel}\", \"manufacturer\":\"RPI Foundation\"}},"
                    + f"\"icon\":\"mdi:wifi\"}}",
            qos=1,
            retain=True,
        )

    if "external_drives" in settings:
        for drive in settings["external_drives"]:
            mqttClient.publish(
                topic=f"homeassistant/sensor/{deviceName}/disk_use_{drive.lower()}/config",
                payload=f"{{\"name\":\"{deviceNameDisplay} Disk Use {drive}\","
                        + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                        + '"unit_of_measurement":"%",'
                        + f"\"value_template\":\"{{{{value_json.disk_use_{drive.lower()}}}}}\","
                        + f"\"unique_id\":\"{deviceName}_sensor_disk_use_{drive.lower()}\","
                        + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                        + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                        + f"\"name\":\"{deviceNameDisplay} Sensors\",\"model\":\"{deviceModel}\", \"manufacturer\":\"RPI Foundation\"}},"
                        + f"\"icon\":\"mdi:harddisk\"}}",
                qos=1,
                retain=True,
            )

    mqttClient.publish(f"system-sensors/sensor/{deviceName}/availability", "online", retain=True)

def _parser():
    """Generate argument parser"""
    parser = argparse.ArgumentParser()
    parser.add_argument("settings", help="path to the settings file")
    return parser


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        write_message_to_console("Connected to broker")
        print("subscribing : hass/status")
        client.subscribe("hass/status")
        print("subscribing : " + f"system-sensors/sensor/{deviceName}/command")
        client.subscribe(f"system-sensors/sensor/{deviceName}/command")#subscribe
        client.publish(f"system-sensors/sensor/{deviceName}/command", "setup", retain=True)
        client.publish(f"system-sensors/sensor/{deviceName}/availability", "online", retain=True)
    else:
        write_message_to_console("Connection failed")

if __name__ == "__main__":
    args = _parser().parse_args()
    with open(args.settings) as f:
        # use safe_load instead load
        settings = yaml.safe_load(f)
    check_settings(settings)
    DEFAULT_TIME_ZONE = timezone(settings["timezone"])
    if "update_interval" in settings:
        WAIT_TIME_SECONDS = settings["update_interval"]
    mqttClient = mqtt.Client(client_id=settings["client_id"])
    mqttClient.on_connect = on_connect                      #attach function to callback
    mqttClient.on_message = on_message
    deviceName = settings["deviceName"].replace(" ", "").lower()
    deviceNameDisplay = settings["deviceName"]
    deviceModel = get_host_model()
    mqttClient.will_set(f"system-sensors/sensor/{deviceName}/availability", "offline", retain=True)
    if "user" in settings["mqtt"]:
        mqttClient.username_pw_set(
            settings["mqtt"]["user"], settings["mqtt"]["password"]
        )  # Username and pass if configured otherwise you should comment out this
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    if "port" in settings["mqtt"]:
        mqttClient.connect(settings["mqtt"]["hostname"], settings["mqtt"]["port"])
    else:
        mqttClient.connect(settings["mqtt"]["hostname"], 1883)
    try:
        remove_old_topics()
        send_config_message(mqttClient)
    except:
        write_message_to_console("something whent wrong")
    _underVoltage = new_under_voltage()
    # update all sensor values on startup
    updateSensors()
    job = Job(interval=timedelta(seconds=WAIT_TIME_SECONDS), execute=updateSensors)
    job.start()

    mqttClient.loop_start()

    while True:
        try:
            sys.stdout.flush()
            time.sleep(1)
        except ProgramKilled:
            write_message_to_console("Program killed: running cleanup code")
            mqttClient.publish(f"system-sensors/sensor/{deviceName}/availability", "offline", retain=True)
            mqttClient.disconnect()
            mqttClient.loop_stop()
            sys.stdout.flush()
            job.stop()
            break
