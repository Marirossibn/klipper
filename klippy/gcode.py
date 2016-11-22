# Parse gcode commands
#
# Copyright (C) 2016  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import os, re, logging, collections
import homing

# Parse out incoming GCode and find and translate head movements
class GCodeParser:
    RETRY_TIME = 0.100
    def __init__(self, printer, fd, inputfile=False):
        self.printer = printer
        self.fd = fd
        self.inputfile = inputfile
        # Input handling
        self.reactor = printer.reactor
        self.fd_handle = None
        self.input_commands = [""]
        self.need_register_fd = False
        self.bytes_read = 0
        self.input_log = collections.deque([], 50)
        # Busy handling
        self.busy_timer = self.reactor.register_timer(self.busy_handler)
        self.busy_state = None
        # Command handling
        self.gcode_handlers = {}
        self.is_shutdown = False
        self.need_ack = False
        self.toolhead = self.heater_nozzle = self.heater_bed = self.fan = None
        self.speed = 1.0
        self.absolutecoord = self.absoluteextrude = True
        self.base_position = [0.0, 0.0, 0.0, 0.0]
        self.last_position = [0.0, 0.0, 0.0, 0.0]
        self.homing_add = [0.0, 0.0, 0.0, 0.0]
        self.axis2pos = {'X': 0, 'Y': 1, 'Z': 2, 'E': 3}
    def build_config(self):
        self.toolhead = self.printer.objects['toolhead']
        self.heater_nozzle = None
        extruder = self.printer.objects.get('extruder')
        if extruder:
            self.heater_nozzle = extruder.heater
        self.heater_bed = self.printer.objects.get('heater_bed')
        self.fan = self.printer.objects.get('fan')
        self.build_handlers()
    def build_handlers(self):
        shutdown_handlers = ['M105', 'M110', 'M114']
        handlers = ['G0', 'G1', 'G4', 'G20', 'G21', 'G28', 'G90', 'G91', 'G92',
                    'M18', 'M82', 'M83', 'M84', 'M110', 'M114', 'M119', 'M206']
        if self.heater_nozzle is not None:
            handlers.extend(['M104', 'M105', 'M109', 'M303'])
        if self.heater_bed is not None:
            handlers.extend(['M140', 'M190'])
        if self.fan is not None:
            handlers.extend(['M106', 'M107'])
        if self.is_shutdown:
            handlers = [h for h in handlers if h in shutdown_handlers]
        self.gcode_handlers = dict((h, getattr(self, 'cmd_'+h))
                                   for h in handlers)
    def run(self):
        self.fd_handle = self.reactor.register_fd(self.fd, self.process_data)
        self.reactor.run()
    def finish(self):
        self.reactor.end()
        self.toolhead.motor_off()
        logging.debug('Completed translation by klippy')
    def stats(self, eventtime):
        return "gcodein=%d" % (self.bytes_read,)
    def shutdown(self):
        self.is_shutdown = True
        self.build_handlers()
        logging.info("Dumping gcode input %d blocks" % (len(self.input_log),))
        for eventtime, data in self.input_log:
            logging.info("Read %f: %s" % (eventtime, repr(data)))
    # Parse input into commands
    args_r = re.compile('([a-zA-Z*])')
    def process_commands(self, eventtime):
        i = -1
        for i in range(len(self.input_commands)-1):
            line = self.input_commands[i]
            # Ignore comments and leading/trailing spaces
            line = origline = line.strip()
            cpos = line.find(';')
            if cpos >= 0:
                line = line[:cpos]
            # Break command into parts
            parts = self.args_r.split(line)[1:]
            params = dict((parts[i].upper(), parts[i+1].strip())
                          for i in range(0, len(parts), 2))
            params['#original'] = origline
            if parts and parts[0].upper() == 'N':
                # Skip line number at start of command
                del parts[:2]
            if not parts:
                self.cmd_default(params)
                continue
            params['#command'] = cmd = parts[0] + parts[1].strip()
            # Invoke handler for command
            self.need_ack = True
            handler = self.gcode_handlers.get(cmd, self.cmd_default)
            try:
                handler(params)
            except:
                logging.exception("Exception in command handler")
                self.toolhead.force_shutdown()
                self.respond('Error: Internal error on command:"%s"' % (cmd,))
            # Check if machine can process next command or must stall input
            if self.busy_state is not None:
                break
            if self.toolhead.check_busy(eventtime):
                self.set_busy(self.toolhead)
                break
            self.ack()
        del self.input_commands[:i+1]
    def process_data(self, eventtime):
        if self.busy_state is not None:
            self.reactor.unregister_fd(self.fd_handle)
            self.need_register_fd = True
            return
        data = os.read(self.fd, 4096)
        self.input_log.append((eventtime, data))
        self.bytes_read += len(data)
        lines = data.split('\n')
        lines[0] = self.input_commands[0] + lines[0]
        self.input_commands = lines
        self.process_commands(eventtime)
        if not data and self.inputfile:
            self.finish()
    # Response handling
    def ack(self, msg=None):
        if not self.need_ack or self.inputfile:
            return
        if msg:
            os.write(self.fd, "ok %s\n" % (msg,))
        else:
            os.write(self.fd, "ok\n")
        self.need_ack = False
    def respond(self, msg):
        logging.debug(msg)
        if self.inputfile:
            return
        os.write(self.fd, msg+"\n")
    # Busy handling
    def set_busy(self, busy_handler):
        self.busy_state = busy_handler
        self.reactor.update_timer(self.busy_timer, self.reactor.NOW)
    def busy_handler(self, eventtime):
        try:
            busy = self.busy_state.check_busy(eventtime)
        except homing.EndstopError, e:
            self.respond("Error: %s" % (e,))
            busy = False
        except:
            logging.exception("Exception in busy handler")
            self.toolhead.force_shutdown()
            self.respond('Error: Internal error in busy handler')
            busy = False
        if busy:
            self.toolhead.reset_motor_off_time(eventtime)
            return eventtime + self.RETRY_TIME
        self.busy_state = None
        self.ack()
        self.process_commands(eventtime)
        if self.busy_state is not None:
            return self.reactor.NOW
        if self.need_register_fd:
            self.need_register_fd = False
            self.fd_handle = self.reactor.register_fd(self.fd, self.process_data)
        return self.reactor.NEVER
    # Temperature wrappers
    def get_temp(self):
        # T:XXX /YYY B:XXX /YYY
        out = []
        if self.heater_nozzle:
            cur, target = self.heater_nozzle.get_temp()
            out.append("T:%.1f /%.1f" % (cur, target))
        if self.heater_bed:
            cur, target = self.heater_bed.get_temp()
            out.append("B:%.1f /%.1f" % (cur, target))
        return " ".join(out)
    def bg_temp(self, heater):
        # Wrapper class for check_busy() that periodically prints current temp
        class temp_busy_handler_wrapper:
            gcode = self
            last_temp_time = 0.
            cur_heater = heater
            def check_busy(self, eventtime):
                if eventtime > self.last_temp_time + 1.0:
                    self.gcode.respond(self.gcode.get_temp())
                    self.last_temp_time = eventtime
                return self.cur_heater.check_busy(eventtime)
        if self.inputfile:
            return
        self.set_busy(temp_busy_handler_wrapper())
    def set_temp(self, heater, params, wait=False):
        print_time = self.toolhead.get_last_move_time()
        temp = float(params.get('S', '0'))
        heater.set_temp(print_time, temp)
        if wait:
            self.bg_temp(heater)
    # Individual command handlers
    def cmd_default(self, params):
        if self.is_shutdown:
            self.respond('Error: Machine is shutdown')
            return
        cmd = params.get('#command')
        if not cmd:
            logging.debug(params['#original'])
            return
        self.respond('echo:Unknown command:"%s"' % (cmd,))
    def cmd_G0(self, params):
        self.cmd_G1(params, sloppy=True)
    def cmd_G1(self, params, sloppy=False):
        # Move
        for a, p in self.axis2pos.items():
            if a in params:
                v = float(params[a])
                if not self.absolutecoord or (p>2 and not self.absoluteextrude):
                    # value relative to position of last move
                    self.last_position[p] += v
                else:
                    # value relative to base coordinate position
                    self.last_position[p] = v + self.base_position[p]
        if 'F' in params:
            self.speed = float(params['F']) / 60.
        try:
            self.toolhead.move(self.last_position, self.speed, sloppy)
        except homing.EndstopError, e:
            self.respond("Error: %s" % (e,))
            self.last_position = self.toolhead.get_position()
    def cmd_G4(self, params):
        # Dwell
        if 'S' in params:
            delay = float(params['S'])
        else:
            delay = float(params.get('P', '0')) / 1000.
        self.toolhead.dwell(delay)
    def cmd_G20(self, params):
        # Set units to inches
        self.respond('Error: Machine does not support G20 (inches) command')
    def cmd_G21(self, params):
        # Set units to millimeters
        pass
    def cmd_G28(self, params):
        # Move to origin
        axes = []
        for axis in 'XYZ':
            if axis in params:
                axes.append(self.axis2pos[axis])
        if not axes:
            axes = [0, 1, 2]
        homing_state = homing.Homing(self.toolhead, axes)
        if self.inputfile:
            homing_state.set_no_verify_retract()
        self.toolhead.home(homing_state)
        def axes_update(homing_state):
            newpos = self.toolhead.get_position()
            for axis in homing_state.get_axes():
                self.last_position[axis] = newpos[axis]
                self.base_position[axis] = -self.homing_add[axis]
        homing_state.plan_axes_update(axes_update)
        self.set_busy(homing_state)
    def cmd_G90(self, params):
        # Use absolute coordinates
        self.absolutecoord = True
    def cmd_G91(self, params):
        # Use relative coordinates
        self.absolutecoord = False
    def cmd_G92(self, params):
        # Set position
        mcount = 0
        for a, p in self.axis2pos.items():
            if a in params:
                self.base_position[p] = self.last_position[p] - float(params[a])
                mcount += 1
        if not mcount:
            self.base_position = list(self.last_position)
    def cmd_M82(self, params):
        # Use absolute distances for extrusion
        self.absoluteextrude = True
    def cmd_M83(self, params):
        # Use relative distances for extrusion
        self.absoluteextrude = False
    def cmd_M18(self, params):
        # Turn off motors
        self.toolhead.motor_off()
    def cmd_M84(self, params):
        # Stop idle hold
        self.toolhead.motor_off()
    def cmd_M105(self, params):
        # Get Extruder Temperature
        self.ack(self.get_temp())
    def cmd_M104(self, params):
        # Set Extruder Temperature
        self.set_temp(self.heater_nozzle, params)
    def cmd_M109(self, params):
        # Set Extruder Temperature and Wait
        self.set_temp(self.heater_nozzle, params, wait=True)
    def cmd_M110(self, params):
        # Set Current Line Number
        pass
    def cmd_M114(self, params):
        # Get Current Position
        kinpos = self.toolhead.get_position()
        self.respond("X:%.3f Y:%.3f Z:%.3f E:%.3f Count X:%.3f Y:%.3f Z:%.3f" % (
            self.last_position[0], self.last_position[1],
            self.last_position[2], self.last_position[3],
            kinpos[0], kinpos[1], kinpos[2]))
    def cmd_M119(self, params):
        # Get Endstop Status
        if self.inputfile:
            return
        print_time = self.toolhead.get_last_move_time()
        query_state = homing.QueryEndstops(print_time, self.respond)
        self.toolhead.query_endstops(query_state)
        self.set_busy(query_state)
    def cmd_M140(self, params):
        # Set Bed Temperature
        self.set_temp(self.heater_bed, params)
    def cmd_M190(self, params):
        # Set Bed Temperature and Wait
        self.set_temp(self.heater_bed, params, wait=True)
    def cmd_M106(self, params):
        # Set fan speed
        print_time = self.toolhead.get_last_move_time()
        self.fan.set_speed(print_time, float(params.get('S', '255')) / 255.)
    def cmd_M107(self, params):
        # Turn fan off
        print_time = self.toolhead.get_last_move_time()
        self.fan.set_speed(print_time, 0)
    def cmd_M206(self, params):
        # Set home offset
        for a, p in self.axis2pos.items():
            if a in params:
                v = float(params[a])
                self.base_position[p] += self.homing_add[p] - v
                self.homing_add[p] = v
    def cmd_M303(self, params):
        # Run PID tuning
        heater = int(params.get('E', '0'))
        heater = {0: self.heater_nozzle, -1: self.heater_bed}[heater]
        temp = float(params.get('S', '60'))
        heater.start_auto_tune(temp)
        self.bg_temp(heater)
