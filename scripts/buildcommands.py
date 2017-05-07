#!/usr/bin/env python
# Script to handle build time requests embedded in C code.
#
# Copyright (C) 2016  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import sys, os, subprocess, optparse, logging, shlex, socket, time
import json, zlib
sys.path.append('./klippy')
import msgproto

FILEHEADER = """
/* DO NOT EDIT!  This is an autogenerated file.  See scripts/buildcommands.py. */

#include "board/pgm.h"
#include "command.h"
#include "compiler.h"
"""

def error(msg):
    sys.stderr.write(msg + "\n")
    sys.exit(-1)


######################################################################
# Command and output parser generation
######################################################################

def build_parser(parser, iscmd, all_param_types):
    if parser.name == "#empty":
        return "\n    // Empty message\n    .max_size=0,"
    if parser.name == "#output":
        comment = "Output: " + parser.msgformat
    else:
        comment = parser.msgformat
    params = '0'
    types = tuple([t.__class__.__name__ for t in parser.param_types])
    if types:
        paramid = all_param_types.get(types)
        if paramid is None:
            paramid = len(all_param_types)
            all_param_types[types] = paramid
        params = 'command_parameters%d' % (paramid,)
    out = """
    // %s
    .msg_id=%d,
    .num_params=%d,
    .param_types = %s,
""" % (comment, parser.msgid, len(types), params)
    if iscmd:
        num_args = (len(types) + types.count('PT_progmem_buffer')
                    + types.count('PT_buffer'))
        out += "    .num_args=%d," % (num_args,)
    else:
        max_size = min(msgproto.MESSAGE_MAX - msgproto.MESSAGE_MIN
                       , 1 + sum([t.max_length for t in parser.param_types]))
        out += "    .max_size=%d," % (max_size,)
    return out

def build_parsers(parsers, msg_to_id, all_param_types):
    pcode = []
    for msgname, msg in parsers:
        msgid = msg_to_id[msg]
        if msgname is None:
            parser = msgproto.OutputFormat(msgid, msg)
        else:
            parser = msgproto.MessageFormat(msgid, msg)
        parsercode = build_parser(parser, 0, all_param_types)
        pcode.append("{%s\n}, " % (parsercode,))
    fmt = """
const struct command_encoder command_encoders[] PROGMEM __visible = {
%s
};
"""
    return fmt % ("".join(pcode).strip(),)

def build_param_types(all_param_types):
    sorted_param_types = sorted([(i, a) for a, i in all_param_types.items()])
    params = ['']
    for paramid, argtypes in sorted_param_types:
        params.append(
            'static const uint8_t command_parameters%d[] PROGMEM = {\n'
            '    %s };' % (
                paramid, ', '.join(argtypes),))
    params.append('')
    return "\n".join(params)

def build_commands(cmd_by_id, messages_by_name, all_param_types):
    max_cmd_msgid = max(cmd_by_id.keys())
    index = []
    parsers = []
    externs = {}
    for msgid in range(max_cmd_msgid+1):
        if msgid not in cmd_by_id:
            index.append("    0,")
            continue
        funcname, flags, msgname = cmd_by_id[msgid]
        msg = messages_by_name[msgname]
        externs[funcname] = 1
        parsername = 'parser_%s' % (funcname,)
        index.append("    &%s," % (parsername,))
        parser = msgproto.MessageFormat(msgid, msg)
        parsercode = build_parser(parser, 1, all_param_types)
        parsers.append("const struct command_parser %s PROGMEM __visible = {"
                       "    %s\n    .flags=%s,\n    .func=%s\n};" % (
                           parsername, parsercode, flags, funcname))
    index = "\n".join(index)
    externs = "\n".join(["extern void "+funcname+"(uint32_t*);"
                         for funcname in sorted(externs)])
    fmt = """
%s

%s

const struct command_parser * const command_index[] PROGMEM __visible = {
%s
};

const uint8_t command_index_size PROGMEM __visible = ARRAY_SIZE(command_index);
"""
    return fmt % (externs, '\n'.join(parsers), index)


######################################################################
# Identify data dictionary generation
######################################################################

def build_identify(cmd_by_id, msg_to_id, responses, static_strings
                   , constants, version):
    #commands, messages, static_strings
    messages = dict((msgid, msg) for msg, msgid in msg_to_id.items())
    data = {}
    data['messages'] = messages
    data['commands'] = sorted(cmd_by_id.keys())
    data['responses'] = sorted(responses)
    data['static_strings'] = static_strings
    data['config'] = constants
    data['version'] = version

    # Format compressed info into C code
    data = json.dumps(data)
    zdata = zlib.compress(data, 9)
    out = []
    for i in range(len(zdata)):
        if i % 8 == 0:
            out.append('\n   ')
        out.append(" 0x%02x," % (ord(zdata[i]),))
    fmt = """
const uint8_t command_identify_data[] PROGMEM __visible = {%s
};

// Identify size = %d (%d uncompressed)
const uint32_t command_identify_size PROGMEM __visible
    = ARRAY_SIZE(command_identify_data);
"""
    return data, fmt % (''.join(out), len(zdata), len(data))


######################################################################
# Version generation
######################################################################

# Run program and return the specified output
def check_output(prog):
    logging.debug("Running %s" % (repr(prog),))
    try:
        process = subprocess.Popen(shlex.split(prog), stdout=subprocess.PIPE)
        output = process.communicate()[0]
        retcode = process.poll()
    except OSError:
        logging.debug("Exception on run: %s" % (traceback.format_exc(),))
        return ""
    logging.debug("Got (code=%s): %s" % (retcode, repr(output)))
    if retcode:
        return ""
    try:
        return output.decode()
    except UnicodeError:
        logging.debug("Exception on decode: %s" % (traceback.format_exc(),))
        return ""

# Obtain version info from "git" program
def git_version():
    if not os.path.exists('.git'):
        logging.debug("No '.git' file/directory found")
        return ""
    ver = check_output("git describe --tags --long --dirty").strip()
    logging.debug("Got git version: %s" % (repr(ver),))
    return ver

def build_version(extra):
    version = git_version()
    if not version:
        version = "?"
    btime = time.strftime("%Y%m%d_%H%M%S")
    hostname = socket.gethostname()
    version = "%s-%s-%s%s" % (version, btime, hostname, extra)
    return version


######################################################################
# Main code
######################################################################

def main():
    usage = "%prog [options] <cmd section file> <output.c>"
    opts = optparse.OptionParser(usage)
    opts.add_option("-e", "--extra", dest="extra", default="",
                    help="extra version string to append to version")
    opts.add_option("-d", dest="write_dictionary",
                    help="file to write mcu protocol dictionary")
    opts.add_option("-v", action="store_true", dest="verbose",
                    help="enable debug messages")

    options, args = opts.parse_args()
    if len(args) != 2:
        opts.error("Incorrect arguments")
    incmdfile, outcfile = args
    if options.verbose:
        logging.basicConfig(level=logging.DEBUG)

    # Setup
    commands = {}
    messages_by_name = dict((m.split()[0], m)
                            for m in msgproto.DefaultMessages.values())
    parsers = []
    static_strings = []
    constants = {}
    # Parse request file
    f = open(incmdfile, 'rb')
    data = f.read()
    f.close()
    for req in data.split('\0'):
        req = req.lstrip()
        parts = req.split()
        if not parts:
            continue
        cmd = parts[0]
        msg = req[len(cmd)+1:]
        if cmd == '_DECL_COMMAND':
            funcname, flags, msgname = parts[1:4]
            if msgname in commands:
                error("Multiple definitions for command '%s'" % msgname)
            commands[msgname] = (funcname, flags, msgname)
            msg = req.split(None, 3)[3]
            m = messages_by_name.get(msgname)
            if m is not None and m != msg:
                error("Conflicting definition for command '%s'" % msgname)
            messages_by_name[msgname] = msg
        elif cmd == '_DECL_PARSER':
            if len(parts) == 1:
                msgname = msg = "#empty"
            else:
                msgname = parts[1]
            m = messages_by_name.get(msgname)
            if m is not None and m != msg:
                error("Conflicting definition for message '%s'" % msgname)
            messages_by_name[msgname] = msg
            parsers.append((msgname, msg))
        elif cmd == '_DECL_OUTPUT':
            parsers.append((None, msg))
        elif cmd == '_DECL_STATIC_STR':
            static_strings.append(req[17:])
        elif cmd == '_DECL_CONSTANT':
            name, value = parts[1:]
            value = value.strip()
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            if name in constants and constants[name] != value:
                error("Conflicting definition for constant '%s'" % name)
            constants[name] = value
        else:
            error("Unknown build time command '%s'" % cmd)
    # Create unique ids for each message type
    msgid = max(msgproto.DefaultMessages.keys())
    msg_to_id = dict((m, i) for i, m in msgproto.DefaultMessages.items())
    for msgname in commands.keys() + [m for n, m in parsers]:
        msg = messages_by_name.get(msgname, msgname)
        if msg not in msg_to_id:
            msgid += 1
            msg_to_id[msg] = msgid
    # Create message definitions
    all_param_types = {}
    parsercode = build_parsers(parsers, msg_to_id, all_param_types)
    # Create command definitions
    cmd_by_id = dict((msg_to_id[messages_by_name.get(msgname, msgname)], cmd)
                     for msgname, cmd in commands.items())
    cmdcode = build_commands(cmd_by_id, messages_by_name, all_param_types)
    paramcode = build_param_types(all_param_types)
    # Create identify information
    version = build_version(options.extra)
    sys.stdout.write("Version: %s\n" % (version,))
    responses = [msg_to_id[msg] for msgname, msg in messages_by_name.items()
                 if msgname not in commands]
    datadict, icode = build_identify(cmd_by_id, msg_to_id, responses
                                     , static_strings, constants, version)
    # Write output
    f = open(outcfile, 'wb')
    f.write(FILEHEADER + paramcode + parsercode + cmdcode + icode)
    f.close()

    # Write data dictionary
    if options.write_dictionary:
        f = open(options.write_dictionary, 'wb')
        f.write(datadict)
        f.close()

if __name__ == '__main__':
    main()
