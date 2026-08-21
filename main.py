import json
import network
import ntptime
import time
import gc
import machine
from machine import Pin, unique_id
import urequests
from machine import reset

from galactic import GalacticUnicorn
from picographics import PicoGraphics, DISPLAY_GALACTIC_UNICORN as DISPLAY
#from setup_portal import SetupPortal

VERSION = "0.0.1"

# overclock to 200Mhz
#machine.freq(200000000)

# create galactic object and graphics surface for drawing
galactic = GalacticUnicorn()
graphics = PicoGraphics(DISPLAY)

brightness = 0.5

MINS_TO_DEPARTURE_GO_RED_THRESHOLD = 10

class PicoDepartureBoard:

    WIFI_MINIMUM_CONNECTION_ATTEMPTS = 0
    WIFI_MAXIMUM_CONNECTION_ATTEMPTS = 60
    DEPARTURE_REFRESH_SECONDS = 60
    API_TIMEOUT_SECONDS = 10
    DARWIN_ENDPOINT = "https://lite.realtime.nationalrail.co.uk/OpenLDBWS/ldb12.asmx"
    FETCH_NUM_ROWS = 10
    SELECTED_PLATFORM = 3 #Replace with selected platform at your own station

    # Sync time at 02:00 UTC daily (after 01:00 BST changeover and hopefully less
    # noticable/jarring in the middle of the night if time has drifted slightly
    # and needs to be corrected)
    TIME_SYNC_HOUR_UTC = 2

    def __init__(self):
        self.status_led = Pin("LED", Pin.OUT)
        self.status_led.value(True)


        # Load API credentials
        api_creds = self._load_json_config(
            "api.json",
            [
                "api_token",
                "station_code",
                "platform",
                "station_name",
                "show_splash_screens",
            ],
        )
        self.api_token = api_creds["api_token"]
        self.station_code = api_creds["station_code"].upper()
        self.station_name = api_creds["station_name"]
        self.platform = api_creds["platform"]
        self.show_splash_screens = api_creds["show_splash_screens"]



    def _load_json_config(self, filename, required_keys):
        try:
            with open(filename, "r") as f:
                data = json.load(f)
        except OSError:
            print("Missing file", filename)
            raise
        except ValueError:
            print("Invalid JSON", filename)
            raise

        for key in required_keys:
            if key not in data:
                raise KeyError(f"Missing '{key}' in {filename}")

        return data


    def _build_departures_request(self, num_rows=3):
        return (
            '<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"'
            ' xmlns:typ="http://thalesgroup.com/RTTI/2013-11-28/Token/types"'
            ' xmlns:ldb="http://thalesgroup.com/RTTI/2021-11-01/ldb/">'
            "<soap:Header><typ:AccessToken>"
            "<typ:TokenValue>{}</typ:TokenValue>"
            "</typ:AccessToken></soap:Header>"
            "<soap:Body><ldb:GetDepartureBoardRequest>"
            "<ldb:numRows>{}</ldb:numRows>"
            "<ldb:crs>{}</ldb:crs>"
            "</ldb:GetDepartureBoardRequest></soap:Body>"
            "</soap:Envelope>"
        ).format(self.api_token, num_rows, self.station_code)

    def _build_service_request(self, service_id):
        return (
            '<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"'
            ' xmlns:typ="http://thalesgroup.com/RTTI/2013-11-28/Token/types"'
            ' xmlns:ldb="http://thalesgroup.com/RTTI/2021-11-01/ldb/">'
            "<soap:Header><typ:AccessToken>"
            "<typ:TokenValue>{}</typ:TokenValue>"
            "</typ:AccessToken></soap:Header>"
            "<soap:Body><ldb:GetServiceDetailsRequest>"
            "<ldb:serviceID>{}</ldb:serviceID>"
            "</ldb:GetServiceDetailsRequest></soap:Body>"
            "</soap:Envelope>"
        ).format(self.api_token, service_id)

    def _find_tag_value(self, xml, tag, start=0):
        # Find <prefix:tag> or <tag> and extract content up to closing tag
        # Search for :tag> first (namespaced), then <tag> (unnamespaced)
        needle = ":" + tag + ">"
        pos = xml.find(needle, start)
        if pos < 0:
            needle = "<" + tag + ">"
            pos = xml.find(needle, start)
            if pos < 0:
                return None, start
        val_start = pos + len(needle)
        # Find closing tag - look for </...tag>
        end = xml.find(tag + ">", val_start)
        if end < 0:
            return None, start
        # Walk back to find the </  or </:
        val_end = end
        while (
            val_end > val_start and xml[val_end - 1] != "<" and xml[val_end - 1] != ":"
        ):
            val_end -= 1
        if val_end <= val_start:
            return None, start
        # val_end - 1 is either < or :, we want everything before that
        if xml[val_end - 1] == ":":
            # </prefix:tag> - go back one more to find <
            val_end -= 1
            while val_end > val_start and xml[val_end - 1] != "<":
                val_end -= 1
        # val_end - 1 should be '<' now (the '<' of the closing tag)
        value = xml[val_start : val_end - 1]
        # Unescape XML entities
        if "&" in value:
            value = value.replace("&amp;", "&")
            value = value.replace("&lt;", "<")
            value = value.replace("&gt;", ">")
            value = value.replace("&apos;", "'")
            value = value.replace("&quot;", '"')
        after = end + len(tag) + 1
        return value, after

    def _find_all_blocks(self, xml, tag):
        pos = 0
        while True:
            # Find opening tag with namespace prefix or without
            needle = ":" + tag + ">"
            start = xml.find(needle, pos)
            if start < 0:
                needle = "<" + tag + ">"
                start = xml.find(needle, pos)
                if start < 0:
                    break
            block_start = start + len(needle)
            # Find closing tag
            close_needle = tag + ">"
            end = xml.find(close_needle, block_start)
            # Walk backwards to find </ for the closing tag
            scan = end - 1
            while scan >= block_start and xml[scan] != "<":
                scan -= 1
            if scan < block_start:
                break
            yield xml[block_start:scan]
            pos = end + len(close_needle)

    def connect_to_wifi(self):
        wifi_creds = self._load_json_config("wifi.json", ["ssid", "password"])
        ssid = wifi_creds["ssid"]
        password = wifi_creds["password"]
        print("WiFi creds:", wifi_creds)

        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        wlan.connect(ssid, password)

        connection_attempt = 0
        while connection_attempt < self.WIFI_MAXIMUM_CONNECTION_ATTEMPTS:
            if connection_attempt > self.WIFI_MINIMUM_CONNECTION_ATTEMPTS and (
                wlan.status() < 0 or wlan.status() >= 3
            ):
                break
            connection_attempt += 1

            print("Connecting to", ssid, f"Attempt {connection_attempt}")
            self.status_led.toggle()

            time.sleep(1)

        if wlan.status() < 0:
            raise Exception("Connection failed")

        self.sync_time()

        if not self.show_splash_screens:
            return

        print("Pico Departure", f"Board v{VERSION}", wlan.ifconfig()[0])
        time.sleep(5)

    def fetch_departures(self):
        print("Fetching departures from National Rail API")
        body = self._build_departures_request(self.FETCH_NUM_ROWS)
        headers = {"Content-Type": "application/soap+xml; charset=utf-8"}

        try:
            response = urequests.post(
                self.DARWIN_ENDPOINT,
                data=body,
                headers=headers,
                timeout=self.API_TIMEOUT_SECONDS,
            )
            if response.status_code != 200:
                raise Exception(f"API error: {response.status_code}")
        except Exception as e:
            print(f"API error: {e}")
            time.sleep(5)
            return None

        gc.collect()
        xml = response.text
        response.close()
        gc.collect()

        services = []
        for block in self._find_all_blocks(xml, "service"):
            std, _ = self._find_tag_value(block, "std")
            etd, _ = self._find_tag_value(block, "etd")
            platform, _ = self._find_tag_value(block, "platform")
            service_id, _ = self._find_tag_value(block, "serviceID")

            if self.platform and platform != self.platform:
                continue

            # Destination name is nested inside a destination > location block
            dest_name = None
            for dest_block in self._find_all_blocks(block, "destination"):
                dest_name, _ = self._find_tag_value(dest_block, "locationName")
                if not dest_name:
                    dest_name, _ = self._find_tag_value(dest_block, "name")
                break

            service = {
                "std": std or "??:??",
                "etd": etd or "",
                "serviceID": service_id or "",
                "destination": [{"locationName": dest_name or ""}],
            }
            if platform:
                service["platform"] = platform
            services.append(service)

        if services:
            return {"trainServices": services}
        return {"trainServices": None}

    def fetch_calling_points(self, service_id):
        print(f"Fetching calling points for {service_id}")
        body = self._build_service_request(service_id)
        headers = {"Content-Type": "application/soap+xml; charset=utf-8"}

        try:
            response = urequests.post(
                self.DARWIN_ENDPOINT,
                data=body,
                headers=headers,
                timeout=self.API_TIMEOUT_SECONDS,
            )
            if response.status_code != 200:
                return []
        except Exception as e:
            print(f"Calling points fetch failed: {e}")
            return []

        gc.collect()
        xml = response.text
        response.close()
        gc.collect()

        # Find the subsequentCallingPoints section
        points = []
        for scp_block in self._find_all_blocks(xml, "subsequentCallingPoints"):
            for cp_block in self._find_all_blocks(scp_block, "callingPoint"):
                name, _ = self._find_tag_value(cp_block, "locationName")
                if name:
                    points.append(name)
            break  # only first subsequentCallingPoints list
        return points

    def _last_sunday_of_month(self, year, month):
        """
        Return day of month of the last Sunday in a given month
        """
        # Find last day of the month
        if month == 12:
            next_month = 1
            next_year = year + 1
        else:
            next_month = month + 1
            next_year = year
        # Last day = day before the 1st of next month
        t = time.mktime((next_year, next_month, 1, 0, 0, 0, 0, 0))
        t -= 24 * 60 * 60  # subtract one day
        last_day_info = time.localtime(t)
        last_day = last_day_info[2]

        # Back up to Sunday (MicroPython: 0=Mon, 6=Sun)
        days_since_sunday = (last_day_info[6] + 1) % 7
        return last_day - days_since_sunday

    def sync_time(self):
        """
        Sync clock via NTP and determine whether we're in BST.
        BST starts at 01:00 UTC on the last Sunday of March
        and ends at 01:00 UTC on the last Sunday of October.
        """
        try:
            ntptime.settime()
        except Exception as e:
            # Not the end of the world, we'll try again tomorrow!
            print(f"NTP sync failed: {e}")

        year = time.localtime()[0]
        bst_start = time.mktime(
            (year, 3, self._last_sunday_of_month(year, 3), 1, 0, 0, 0, 0)
        )
        bst_end = time.mktime(
            (year, 10, self._last_sunday_of_month(year, 10), 1, 0, 0, 0, 0)
        )
        now = time.time()
        self.is_bst = bst_start <= now < bst_end
        self._last_sync_date = time.localtime()[:3]  # (year, month, day)
        print(f"Time synced, BST status: {self.is_bst}")

    def _get_current_time(self, include_seconds=False):
        t = time.localtime()
        hour = t[3]
        if self.is_bst:
            # BST is UTC+1, so add 1 hour (and wrap around if necessary for midnight)
            hour = (hour + 1) % 24

        if include_seconds:
            return "{:02d}:{:02d}:{:02d}".format(hour, t[4], t[5])
        else:
            return "{:02d}:{:02d}".format(hour, t[4])

    def _format_etd(self, etd, std):
        # API time can be "Delayed", "On Time", "Cancelled", "No report" or "HH:MM"
        diff = 0
        dep_h, dep_m = map(int, std.split(":"))
        if etd not in (std, "On Time") and all(":" in t for t in (etd, std)):
            # convert HH:MM to minutes to display minutes delayed
            dep_h, dep_m = map(int, etd.split(":"))

        dep_total = dep_h * 60 + dep_m
        t = time.localtime()
        hour = t[3]
        if self.is_bst:
        # BST is UTC+1, so add 1 hour (and wrap around if necessary for midnight)
            hour = (hour + 1) % 24
        current_total = hour * 60 + t[4]
        diff = dep_total - current_total
        if diff < 0:
            diff += 24 * 60
        
        return diff


    def render_departures(self, services, offset=0, calling_at_text=None):

        if not services:
            #Do something to make displays blank
            return

        num_rows = 10
        selected_platform_mins = []
        selected_platform_etd = []
        for current_row in range(num_rows):
            idx = offset + current_row
            if idx >= len(services):
                break

            service = services[idx]
            std = service.get("std", "??:??")  # scheduled time of departure
            
            etd = service.get("etd", "")
            mins = self._format_etd(service.get("etd", ""), std)

            platform = service.get("platform", "")

            # Get destination name, truncate to fit
            dest = ""
            if service.get("destination"):
                dest = service["destination"][0].get("locationName", "")

            line_text = f"Time: {mins} Etd: {etd} Dest: {dest} "

            platform_str = ""
            if platform:
                platform_str = f"Plat {platform}"
            print(line_text + platform_str)
            if platform == str(self.SELECTED_PLATFORM):
                selected_platform_mins.append(mins)
                selected_platform_etd.append(etd)
        
        #Display on LEDs
        graphics.set_font("bitmap8")
        graphics.set_pen(graphics.create_pen(0, 0, 0))
        graphics.clear()
        
        led_text = ""
        
        current_width = -1
        
        # Put current time on screen:
        current_time = self._get_current_time()
        time_width = graphics.measure_text(current_time, 1)
        max_total_width = galactic.WIDTH - time_width
        
        graphics.set_pen(graphics.create_pen(0, 0, 50))
        graphics.rectangle(max_total_width + 1, 0, time_width, 11)
        graphics.set_font("bitmap8")
        
        
        
        graphics.set_pen(graphics.create_pen(255, 255, 255))
        
        graphics.text(current_time, max_total_width + 1, 2, 60, 1)
        
        
        for i, mins_val in enumerate(selected_platform_mins):
            text_width = graphics.measure_text(str(mins_val), 1)
            if (current_width + text_width) <= max_total_width:
                if mins_val < 100: #Only display 2 digit depature mins
                    if mins_val < MINS_TO_DEPARTURE_GO_RED_THRESHOLD:
                        graphics.set_pen(graphics.create_pen(100, 0, 0))
                    elif selected_platform_etd[i] is "On time":
                        graphics.set_pen(graphics.create_pen(52, 40, 80))
                    elif selected_platform_etd[i] is "Delayed":
                        graphics.set_pen(graphics.create_pen(100, 10, 0))
                    else:
                        graphics.set_pen(graphics.create_pen(70, 35, 0))
                    graphics.rectangle(current_width+1, 0, text_width -1, 11)
                    
                    graphics.set_pen(graphics.create_pen(255, 255, 255))
                    graphics.text(str(mins_val), current_width + 1, 2, 60, 1)
                    current_width += text_width + 1 # two column gap
            else:
                current_width = max_total_width


    def show_departure_board(self):
        print("Showing departure board")

        services = []
        offset = 0
        last_fetch = 0

        last_render = time.ticks_ms()


        while True:
            now = time.time()

            # Daily NTP sync and BST recalculation at 02:00 UTC
            t = time.localtime(now)
            if t[3] == self.TIME_SYNC_HOUR_UTC and t[:3] != self._last_sync_date:
                self.sync_time()

            # Refresh data periodically
            if now - last_fetch >= self.DEPARTURE_REFRESH_SECONDS:
                data = self.fetch_departures()
                if data and data.get("trainServices"):
                    services = data["trainServices"]
                    offset = 0
                    print(f"Got {len(services)} services")
                elif data:
                    services = []
                    print("No train services in response")
                else:
                    print("Failed to fetch data, retrying...")

                last_fetch = now
                #self.update_calling_points(services, offset)
                self.render_departures(services, offset)
                

            # brightness up/down
            if galactic.is_pressed(GalacticUnicorn.SWITCH_BRIGHTNESS_UP):
                galactic.adjust_brightness(0.01)
            if galactic.is_pressed(GalacticUnicorn.SWITCH_BRIGHTNESS_DOWN):
                galactic.adjust_brightness(-0.01)
            galactic.update(graphics)





            # Sleep to prevent the CPU from constantly spinning in a tight loop
            time.sleep_ms(10)


if __name__ == "__main__":
    pdb = PicoDepartureBoard()
    pdb.connect_to_wifi()
    pdb.show_departure_board()
