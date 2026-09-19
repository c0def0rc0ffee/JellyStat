# -*- coding: utf-8 -*-
"""
<summary>
A very small Marionette client, so Firefox can be driven with no extras.
</summary>
<remarks>
Firefox ships the protocol; this speaks just enough of it to load a page,
wait for the dashboard to finish fetching, click through the navigation and
take full-page screenshots.
</remarks>
"""

import base64
import json
import socket
import subprocess
import time


class MarionetteError(RuntimeError):
    """
    <summary>
    Anything Firefox or the protocol refused.
    </summary>
    """
    pass


class Firefox(object):
    """
    <summary>
    A Firefox process driven over the Marionette socket.
    </summary>
    """
    def __init__(self, profile, width=1440, height=1000, port=2828,
                 headless=True):
        """
        <summary>
        Launch Firefox with the profile and size, connect, and open a WebDriver session.
        </summary>
        <param name="profile">Profile folder.</param>
        <param name="width">Window width.</param>
        <param name="height">Window height.</param>
        <param name="port">Marionette port.</param>
        <param name="headless">Run without a window.</param>
        """
        self.port = port
        args = ["firefox", "--marionette", "--profile", profile,
                "--window-size=%d,%d" % (width, height)]
        if headless:
            args.insert(1, "--headless")
        self.process = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.socket = self._connect()
        self._id = 0
        self._read()          # the server's hello packet
        self.send("WebDriver:NewSession", {"acceptInsecureCerts": True})

    # -- wire ------------------------------------------------------------
    def _connect(self, timeout=45):
        """
        <summary>
        Poll the Marionette port until Firefox answers.
        </summary>
        <param name="timeout">Seconds to keep trying.</param>
        <returns>A connected socket.</returns>
        <exception cref="MarionetteError">When the port never opens.</exception>
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                connection = socket.create_connection(("127.0.0.1", self.port),
                                                      timeout=10)
                connection.settimeout(180)
                return connection
            except OSError:
                time.sleep(0.4)
        raise MarionetteError("Firefox never opened its Marionette port.")

    def _read(self):
        """
        <summary>
        Read one length prefixed message.
        </summary>
        <returns>The decoded JSON.</returns>
        <exception cref="MarionetteError">When Firefox closes the connection.</exception>
        """
        digits = b""
        while not digits.endswith(b":"):
            chunk = self.socket.recv(1)
            if not chunk:
                raise MarionetteError("Firefox closed the connection.")
            digits += chunk
        length = int(digits[:-1])
        body = b""
        while len(body) < length:
            body += self.socket.recv(length - len(body))
        return json.loads(body.decode("utf-8"))

    def send(self, command, params=None):
        """
        <summary>
        Send a command and wait for its reply.
        </summary>
        <param name="command">WebDriver command name.</param>
        <param name="params">Its parameters, or None.</param>
        <returns>The reply value.</returns>
        <exception cref="MarionetteError">On an error reply.</exception>
        """
        self._id += 1
        blob = json.dumps([0, self._id, command, params or {}]).encode("utf-8")
        self.socket.sendall(b"%d:%s" % (len(blob), blob))
        while True:
            message = self._read()
            if message[0] == 1 and message[1] == self._id:
                break
        if message[2]:
            raise MarionetteError("%s: %s" % (command, message[2]))
        return message[3]

    # -- driving ---------------------------------------------------------
    def navigate(self, url):
        """
        <summary>
        Load a URL.
        </summary>
        <param name="url">The address.</param>
        """
        self.send("WebDriver:Navigate", {"url": url})

    def script(self, body, args=None):
        """
        <summary>
        Run JavaScript in the page and return its value.
        </summary>
        <param name="body">The script.</param>
        <param name="args">Arguments, or None.</param>
        <returns>The value.</returns>
        """
        return self.send("WebDriver:ExecuteScript",
                         {"script": body, "args": args or []})["value"]

    def set_size(self, width, height):
        """
        <summary>
        Resize the window.
        </summary>
        <param name="width">Pixels.</param>
        <param name="height">Pixels.</param>
        """
        self.send("WebDriver:SetWindowRect", {"width": width, "height": height})

    def wait_for(self, expression, timeout=90, poll=0.35):
        """
        <summary>
        Poll a JavaScript expression until it is true.
        </summary>
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.script("return !!(%s)" % expression):
                    return True
            except MarionetteError:
                pass
            time.sleep(poll)
        return False

    def screenshot(self, path, full=True):
        """
        <summary>
        Save a screenshot, full page by default.
        </summary>
        <param name="path">Output file.</param>
        <param name="full">Whole page rather than the viewport.</param>
        <returns>The path.</returns>
        """
        data = self.send("WebDriver:TakeScreenshot",
                         {"full": full, "hash": False})["value"]
        with open(path, "wb") as handle:
            handle.write(base64.b64decode(data))
        return path

    def quit(self):
        """
        <summary>
        Ask Firefox to quit, close the socket, and kill it if it lingers.
        </summary>
        """
        try:
            self.send("Marionette:Quit", {})
        except Exception:
            pass
        try:
            self.socket.close()
        except Exception:
            pass
        try:
            self.process.wait(timeout=20)
        except Exception:
            self.process.kill()
