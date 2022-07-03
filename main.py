# SPDX-FileCopyrightText: 2022 Michael Weiblen http://mew.cx/
#
# SPDX-License-Identifier: MIT

# dust_main.py

'''
This is the main application for collecting sensor data from our "dust"
weather station and saving those data via syslog to our "pink" file server.
The data (and column headers labeling the data) are formatted as familiar
CSV (comma separated value) text for convenient reading into a spreadsheet.

In this system, the syslog facility "local3" is dedicated for use with
the dust weather station.  The CSV data is transmitted via a wifi TCP socket
using syslog's "local3.info" priority (facility "local3", severity "info").
Other non-data messages (e.g. status, error) will use a different severity.

On the receiving end, pink's syslog is configured to write "local3.info"
CSV data messages to the /var/www/html/dust/logs/local3.csv file, which
is accessible using pink's webserver at http://pink/dust/logs/
Any "local3" messages with different severity (i.e. not "info") will be
written to a separate /var/log/local3.log file.
See /etc/rsyslog.d/local3.conf for configuration details.

To prevent those datafiles from growing forever, pink's logrotate periodically
archives collected data and creates empty files for new data.
See /etc/logrotate.d/rsyslog-local3 for configuration details.

See hardware_notes.txt for sensor and interconnection details.
'''

import busio
import time
import board
import atexit
import digitalio
import microcontroller
import gc
import sys
import micropython
from micropython import const

import neopixel
import adafruit_ds1307
import adafruit_dotstar
import adafruit_htu21d
import adafruit_mpl3115a2
from adafruit_sps30.i2c import SPS30_I2C

import rfc5424
import wifi_socket
from secrets import secrets

micropython.opt_level(0)

#############################################################################

class TheApp:
    "The top-level application code for the 'dust' weather station"

    def __init__(self):
        self.dots       = None          # string of dotstar LEDs
        self.ds1307     = None          # battery-backed real-time clock
        self.htu21d     = None          # humidity/temperature sensor
        self.mpl3115    = None          # barometric pressure sensor
        self.sps30      = None          # particulate matter sensor
        self.ws         = None          # wifi_socket to syslog server
        self.HOST       = const("pink") # syslog server name
        self.PORT       = const(514)    # syslog server port
        self.NUM_DOTS   = const(4)      # how many LEDs in the dotstar string
        self.SLEEP_MINS = const(5)      # sleep between measurements [minutes]

    def SetDots(self, r, g, b):
        for dot in range(self.NUM_DOTS):
            self.dots[dot] = (r,g,b)

    def InitializeDevices(self):
        # hardware SPI controls the 4-LED dotstar strip
        self.dots = adafruit_dotstar.DotStar(board.SCK, board.MOSI, self.NUM_DOTS, brightness=0.1)

        # Turn off onboard D13 red LED to save power
        led = digitalio.DigitalInOut(board.LED)
        led.direction = digitalio.Direction.OUTPUT
        led.value = False

        # Turn off I2C VSENSOR to save power
        i2c_power = digitalio.DigitalInOut(board.I2C_POWER)
        i2c_power.switch_to_input()

        # Turn off onboard NeoPixel to save power
        pixel = neopixel.NeoPixel(board.NEOPIXEL, 1)
        pixel.brightness = 0.0
        pixel.fill((0, 0, 0))
        # TODO disable board.NEOPIXEL_POWER

        # The SPS30 limits the I2C bus rate to maximum of 100kHz
        i2c = busio.I2C(board.SCL, board.SDA, frequency=100000)

        # Create the I2C sensor instances
        self.ds1307  = adafruit_ds1307.DS1307(i2c)        # id 0x68
        self.htu21d  = adafruit_htu21d.HTU21D(i2c)        # id 0x40
        self.mpl3115 = adafruit_mpl3115a2.MPL3115A2(i2c)  # id 0x60
        self.sps30   = SPS30_I2C(i2c, fp_mode=True)       # id 0x69

        # We only want barometric pressure, don't care about altitude.
        # mpl3115.sealevel_pressure = 101325

    def ConnectToSyslog(self):
        "Create a socket to the syslog server"
        self.ws = wifi_socket.WifiSocket(self.HOST, self.PORT)
        self.ws.ConnectToAP(secrets["ssid"], secrets["password"])
        self.ws.ConnectToSocket()

    def WriteToSyslog(self, message, severity=rfc5424.Severity.INFO):
        syslog_msg = rfc5424.FormatSyslog(
            facility = rfc5424.Facility.LOCAL3,
            severity = severity,
            timestamp = rfc5424.FormatTimestamp(self.ds1307.datetime),
            hostname = self.ws.ipaddr,
            app_name = "dust",
            msg = message)
        # TODO handle ECONNECT exception
        # HACK!!! Because we're not using SSL (specified by rfc5424),
        # we need a linefeed to terminate the message.
        self.ws.socket.send(syslog_msg + b'\n')

    def WriteCsvHeaders(self):
        "Write column headers for CSV data via syslog"
        self.WriteToSyslog('"timestamp","temp[C]","RH[%]","pres[pa]","tps[um]",' \
                      '"1.0um mass[ug/m^3]","2.5um mass[ug/m^3]","4.0um mass[ug/m^3]",' \
                      '"10um mass[ug/m^3]",' \
                      '"0.5um count[#/cm^3]","1.0um count[#/cm^3]","2.5um count[#/cm^3]",' \
                      '"4.0um count[#/cm^3]","10um count[#/cm^3]"')

    def WriteCsvData(self, csv_msg):
        "Write sensor data in CSV format via syslog"
        self.WriteToSyslog(csv_msg)

    def AcquireData(self):

        ts = rfc5424.FormatTimestamp(self.ds1307.datetime)

        h = "{:0.1f},{:0.1f},{:0.0f},".format(
            self.htu21d.temperature,
            self.htu21d.relative_humidity,
            self.mpl3115.pressure)

        x = self.sps30.read()
#        try:
#            x = self.sps30.read()
#        except RuntimeError as ex:
#            print("Cant read SPS30, skipping: " + str(ex))
#            continue

        p1 = "{:0.3f},".format(x["tps"])
        p2 = "{:0.1f},{:0.1f},{:0.1f},{:0.1f},".format(
            x["pm10 standard"], x["pm25 standard"], x["pm40 standard"], x["pm100 standard"])
        p3 = "{:0.0f},{:0.0f},{:0.0f},{:0.0f},{:0.0f}".format(
            x["particles 05um"], x["particles 10um"], x["particles 25um"],
            x["particles 40um"], x["particles 100um"])

        result = '"' + ts + '",' + h + p1 + p2 + p3
        return result

    def Sleep(self):
        for _ in range(self.SLEEP_MINS):
            time.sleep(60)              # [seconds]
            app.SetDots(0,100,100)      # cyan
            time.sleep(0.1)
            app.SetDots(0,0,0)          # off

    def Shutdown(self):
#        self.WriteToSyslog(severity=rfc5424.Severity.NOTICE,
#            "TheApp.Shutdown")
        self.SetDots(0,0,0)     # off
        # TODO what other shutdown tasks?

#############################################################################
# main

#@atexit.register
#def shutdown():
#    app.Shutdown()

app = TheApp()
app.InitializeDevices()
app.SetDots(0,255,0)            # green
app.ConnectToSyslog()

app.WriteToSyslog("BOOT {} {}".format(
    microcontroller.cpu.reset_reason,
    sys.implementation),
    severity=rfc5424.Severity.NOTICE)

app.WriteCsvHeaders()
while True:
    app.SetDots(0,0,255)        # blue
    result = app.AcquireData()
    app.WriteCsvData(result)
    gc.collect()
    app.SetDots(0,0,0)          # off

    # TODO prepare to sleep
    app.Sleep()
    # TODO wake up from sleep

# vim: set sw=4 ts=8 et ic ai:
