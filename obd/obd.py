# -*- coding: utf-8 -*-

########################################################################
#                                                                      #
# python-OBD: A python OBD-II serial module derived from pyobd         #
#                                                                      #
# Copyright 2004 Donour Sizemore (donour@uchicago.edu)                 #
# Copyright 2009 Secons Ltd. (www.obdtester.com)                       #
# Copyright 2009 Peter J. Creath                                       #
# Copyright 2016 Brendan Whitfield (brendan-w.com)                     #
#                                                                      #
########################################################################
#                                                                      #
# obd.py                                                               #
#                                                                      #
# This file is part of python-OBD (a derivative of pyOBD)              #
#                                                                      #
# python-OBD is free software: you can redistribute it and/or modify   #
# it under the terms of the GNU General Public License as published by #
# the Free Software Foundation, either version 2 of the License, or    #
# (at your option) any later version.                                  #
#                                                                      #
# python-OBD is distributed in the hope that it will be useful,        #
# but WITHOUT ANY WARRANTY; without even the implied warranty of       #
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the        #
# GNU General Public License for more details.                         #
#                                                                      #
# You should have received a copy of the GNU General Public License    #
# along with python-OBD.  If not, see <http://www.gnu.org/licenses/>.  #
#                                                                      #
########################################################################


import logging

from .__version__ import __version__
from .elm327 import ELM327
from .commands import commands
from .OBDResponse import OBDResponse
from .utils import scan_serial, OBDStatus
from .protocols import Message
from binascii import hexlify, unhexlify

logger = logging.getLogger(__name__)


class OBD(object):
    """
        Class representing an OBD-II connection
        with it's assorted commands/sensors.
    """

    def __init__(self, portstr=None, baudrate=None, protocol=None, fast=True):
        self.interface = None
        self.supported_commands = set(commands.base_commands())
        self.fast = fast # global switch for disabling optimizations
        self.__last_command = b"" # used for running the previous command with a CR
        self.__frame_counts = {} # keeps track of the number of return frames for each command

        logger.info("!=================== python-OBD_multi (v%s) ===================!" % __version__)
        self.__connect(portstr, baudrate, protocol) # initialize by connecting and loading sensors
        self.__load_commands()            # try to load the car's supported commands
        logger.info("===================================================================")


    def __connect(self, portstr, baudrate, protocol):
        """
            Attempts to instantiate an ELM327 connection object.
        """

        if portstr is None:
            logger.info("Using scan_serial to select port")
            portnames = scan_serial()
            logger.info("Available ports: " + str(portnames))

            if not portnames:
                logger.warning("No OBD-II adapters found")
                return

            for port in portnames:
                logger.info("Attempting to use port: " + str(port))
                self.interface = ELM327(port, baudrate, protocol)

                if self.interface.status() >= OBDStatus.ELM_CONNECTED:
                    break # success! stop searching for serial
        else:
            logger.info("Explicit port defined")
            self.interface = ELM327(portstr, baudrate, protocol)

        # if the connection failed, close it
        if self.interface.status() == OBDStatus.NOT_CONNECTED:
            # the ELM327 class will report its own errors
            self.close()


    def __load_commands(self):
        """
            Queries for available PIDs, sets their support status,
            and compiles a list of command objects.
        """

        if self.status() != OBDStatus.CAR_CONNECTED:
            logger.warning("Cannot load commands: No connection to car")
            return

        logger.info("querying for supported commands")
        pid_getters = commands.pid_getters()
        for get in pid_getters:
            # PID listing commands should sequentialy become supported
            # Mode 1 PID 0 is assumed to always be supported
            if not self.test_cmd(get, warn=False):
                continue

            # when querying, only use the blocking OBD.query()
            # prevents problems when query is redefined in a subclass (like Async)
            response = OBD.query(self, get)

            if response.is_null():
                logger.info("No valid data for PID listing command: %s" % get)
                continue

            # loop through PIDs bitarray
            for i, bit in enumerate(response.value):
                if bit:

                    mode = get.mode
                    pid  = get.pid + i + 1

                    if commands.has_pid(mode, pid):
                        self.supported_commands.add(commands[mode][pid])

                    # set support for mode 2 commands
                    if mode == 1 and commands.has_pid(2, pid):
                        self.supported_commands.add(commands[2][pid])

        logger.info("finished querying with %d commands supported" % len(self.supported_commands))


    def close(self):
        """
            Closes the connection, and clears supported_commands
        """

        self.supported_commands = set()

        if self.interface is not None:
            logger.info("Closing connection")
            self.interface.close()
            self.interface = None


    def status(self):
        """ returns the OBD connection status """
        if self.interface is None:
            return OBDStatus.NOT_CONNECTED
        else:
            return self.interface.status()


    # not sure how useful this would be

    # def ecus(self):
    #     """ returns a list of ECUs in the vehicle """
    #     if self.interface is None:
    #         return []
    #     else:
    #         return self.interface.ecus()


    def protocol_name(self):
        """ returns the name of the protocol being used by the ELM327 """
        if self.interface is None:
            return ""
        else:
            return self.interface.protocol_name()


    def protocol_id(self):
        """ returns the ID of the protocol being used by the ELM327 """
        if self.interface is None:
            return ""
        else:
            return self.interface.protocol_id()


    def port_name(self):
        """ Returns the name of the currently connected port """
        if self.interface is not None:
            return self.interface.port_name()
        else:
            return ""


    def is_connected(self):
        """
            Returns a boolean for whether a connection with the car was made.

            Note: this function returns False when:
            obd.status = OBDStatus.ELM_CONNECTED
        """
        return self.status() == OBDStatus.CAR_CONNECTED


    def print_commands(self):
        """
            Utility function meant for working in interactive mode.
            Prints all commands supported by the car.
        """
        for c in self.supported_commands:
            print(str(c))


    def supports(self, cmd):
        """
            Returns a boolean for whether the given command
            is supported by the car
        """
        return cmd in self.supported_commands


    def test_cmd(self, cmd, warn=True):
        """
            Returns a boolean for whether a command will
            be sent without using force=True.
        """
        # test if the command is supported
        if not self.supports(cmd):
            if warn:
                logger.warning("'%s' is not supported" % str(cmd))
            return False

        # mode 06 is only implemented for the CAN protocols
        if cmd.mode == 6 and self.interface.protocol_id() not in ["6", "7", "8", "9"]:
            if warn:
                logger.warning("Mode 06 commands are only supported over CAN protocols")
            return False

        return True


    def query(self, cmd, force=False):
        """
            primary API function. Sends commands to the car, and
            protects against sending unsupported commands.
        """

        if self.status() == OBDStatus.NOT_CONNECTED:
            logger.warning("Query failed, no connection available")
            return OBDResponse()

        # if the user forces, skip all checks
        if not force and not self.test_cmd(cmd):
            return OBDResponse()

        # send command and retrieve message
        logger.info("Sending command: %s" % str(cmd))
        cmd_string = self.__build_command_string(cmd)
        messages = self.interface.send_and_parse(cmd_string)

        # if we're sending a new command, note it
        # first check that the current command WASN'T sent as an empty CR
        # (CR is added by the ELM327 class)
        if cmd_string:
            self.__last_command = cmd_string

        # if we don't already know how many frames this command returns,
        # log it, so we can specify it next time
        if cmd not in self.__frame_counts:
            self.__frame_counts[cmd] = sum([len(m.frames) for m in messages])

        if not messages:
            logger.info("No valid OBD Messages returned")
            return OBDResponse()

        return cmd(messages) # compute a response object


    def __build_command_string(self, cmd):
        """ assembles the appropriate command string """
        cmd_string = cmd.command

        # if we know the number of frames that this command returns,
        # only wait for exactly that number. This avoids some harsh
        # timeouts from the ELM, thus speeding up queries.
        if self.fast and cmd.fast and (cmd in self.__frame_counts):
            cmd_string += str(self.__frame_counts[cmd]).encode()

        # if we sent this last time, just send a CR
        # (CR is added by the ELM327 class)
        if self.fast and (cmd_string == self.__last_command):
            cmd_string = b""

        return cmd_string


    def query_multi(self, cmds, force=False):
            """
                primary API function. Sends multiple commands to
                the car for CAN ONLY, and protects against sending
                unsupported commands.

                will (hopefully) return a dict object with cmd:msg
                format.

                -@sommersoft

            """

            if self.status() == OBDStatus.NOT_CONNECTED:
                logger.warning("Query failed, no connection available")
                return OBDResponse()
            elif self.interface.protocol_id() not in ["6", "7", "8", "9"]:
                logger.warning("Multiple PID requests are only supported in"
                            " CAN mode")
                return OBDResponse()
            elif len(cmds) > 6:
                logger.warning("Query failed, too many PIDs requested")
                return OBDResponse()
            elif len(cmds) == 0:
                logger.warning("Query failed, zero PIDs requested")
                return OBDResponse()

            # check each command for support
            # skip tests if forced
            if not force and not all([self.test_cmd(cmd) for cmd in cmds]):
                return

            # check that each command is the same PID mode
            # first PID request will set the main mode
            # good: '> 0104 010B 0111' = '> 01 04 0B 11'
            # bad: '> 0104 020B 0611' = different modes will get chopped
            if not all([cmd.mode == cmds[0].mode for cmd in cmds]):
                logger.warning("commands for query_multi() must be of the same mode")
                return

            # loop through the *cmds list, append them as keys into the
            # cmd_msg dict, build the command string, then send and
            # parse the message updating the cmd_msg dict
            cmd_msg = {}
            cmd_string = cmds[0].command[:2] # mode part
            for cmd in cmds:
                pid_part = cmd.command[2:]
                cmd_msg[pid_part] = cmd.bytes
                cmd_string += pid_part

            # cmd_string built. send off for the response
            logger.info("cmd_string built: %s" % str(cmd_string)) # TODO: remove after testing
            messages = self.interface.send_and_parse(cmd_string)

            if not messages:
                logger.info("No valid OBD Messages returned")
                return OBDResponse()

            
            #logger.info("Message rcvd: %s" % unhexlify(messages.data))  # TODO: remove after testing
            logger.info("cmd_msg{}: %s" % cmd_msg) # TODO: remove after testing
            
            # parse through the returned message finding the associated command
            # and how many bytes the command response is. then construct a response
            # message.
            # @brendan-w wrote this newer version
            master = messages[0] # the message that contains our response
            mode = master.data.pop(0) # the mode byte (ie, for mode 01 this would be 0x41)
            
            cmds_by_pid = { cmd.pid:cmd for cmd in cmds }
            responses = { cmd:OBDResponse() for cmd in cmds }
            
            while len(master.data) > 0:
                pid = master.data[0]
                cmd = cmds_by_pid.get(pid, None)
                print "pid: " + str(pid)
                print "cmd: " + str(cmd.pid)

                # if the PID we pulled out wasn't one of the commands we were given
                # then something is very wrong. Abort, and proceed with whatever
                # we've decoded so far
                if cmd is None:
                    logger.info("Unrequested command answered: %s" % str(pid)) # TODO: remove after testing
                    break
    
                l = cmd.bytes - 1 # this figure INCLUDES the PID byte
                print "l: " + str(l)

                # if the message doesn't have enough data left in it to fulfill a
                # PID, then abort, and proceed with whatever we've decoded so far
                if l > len(master.data):
                    logger.info("Finished parsing query_multi response") # TODO: remove after testing
                    break
            
                # construct a new message
                message = Message(master.frames) # copy of the original lines
                print "pre-chop: " + str(len(message.data))
                message.data = master.data[:l]
                print "post-chop: " + str(len(message.data))
                message.data.insert(0, mode) # prepend the original mode byte
                
                print str(message.data)
            
                # decode the message
                responses[cmd] = cmd(message)
            
                # remove what we just read
                master.data = master.data[l:]
                
            print responses
            #return cmd(messages) # compute a response object
