#!/usr/bin/env python2
# Script to handle build time requests embedded in C code.
#
# Copyright (C) 2016  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import sys, os, subprocess, optparse, logging, shlex, socket, time, traceback
import json, zlib
sys.path.append('./klippy')
import msgproto

FILEHEADER = """
/* DO NOT EDIT!  This is an autogenerated file.  See scripts/buildcommands.py. */

#include "board/irq.h"
#include "board/pgm.h"
#include "command.h"
#include "compiler.h"
"""

def error(msg):
    sys.stderr.write(msg + "\n")
    sys.exit(-1)

Handlers = []


######################################################################
# C call list generation
######################################################################

# Create dynamic C functions that call a list of other C functions
class HandleCallList:
    def __init__(self):
        self.call_lists = {'ctr_run_initfuncs': []}
        self.ctr_dispatch = { '_DECL_CALLLIST': self.decl_calllist }
    def decl_calllist(self, req):
        funcname, callname = req.split()[1:]
        self.call_lists.setdefault(funcname, []).append(callname)
    def update_data_dictionary(self, data):
        pass
    def generate_code(self):
        code = []
        for funcname, funcs in self.call_lists.items():
            func_code = ['    extern void %s(void);\n    %s();' % (f, f)
                         for f in funcs]
            if funcname == 'ctr_run_taskfuncs':
                func_code = ['    irq_poll();\n' + fc for fc in func_code]
            fmt = """
void
%s(void)
{
    %s
}
"""
            code.append(fmt % (funcname, "\n".join(func_code).strip()))
        return "".join(code)

Handlers.append(HandleCallList())


######################################################################
# Static string generation
######################################################################

STATIC_STRING_MIN = 2

# Generate a dynamic string to integer mapping
class HandleStaticStrings:
    def __init__(self):
        self.static_strings = []
        self.ctr_dispatch = { '_DECL_STATIC_STR': self.decl_static_str }
    def decl_static_str(self, req):
        msg = req.split(None, 1)[1]
        self.static_strings.append(msg)
    def update_data_dictionary(self, data):
        data['static_strings'] = { i + STATIC_STRING_MIN: s
                                   for i, s in enumerate(self.static_strings) }
    def generate_code(self):
        code = []
        for i, s in enumerate(self.static_strings):
            code.append('    if (__builtin_strcmp(str, "%s") == 0)\n'
                        '        return %d;\n' % (s, i + STATIC_STRING_MIN))
        fmt = """
uint8_t __always_inline
ctr_lookup_static_string(const char *str)
{
    %s
    return 0xff;
}
"""
        return fmt % ("".join(code).strip(),)

Handlers.append(HandleStaticStrings())


######################################################################
# Constants
######################################################################

# Allow adding build time constants to the data dictionary
class HandleConstants:
    def __init__(self):
        self.constants = {}
        self.ctr_dispatch = { '_DECL_CONSTANT': self.decl_constant }
    def decl_constant(self, req):
        name, value = req.split()[1:]
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        if name in self.constants and self.constants[name] != value:
            error("Conflicting definition for constant '%s'" % name)
        self.constants[name] = value
    def update_data_dictionary(self, data):
        data['config'] = self.constants
    def generate_code(self):
        return ""

Handlers.append(HandleConstants())


######################################################################
# Command and output parser generation
######################################################################

def build_parser(parser, iscmd, all_param_types):
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
        max_size = min(msgproto.MESSAGE_MAX,
                       (msgproto.MESSAGE_MIN + 1
                        + sum([t.max_length for t in parser.param_types])))
        out += "    .max_size=%d," % (max_size,)
    return out

def build_encoders(encoders, msg_to_id, all_param_types):
    encoder_defs = []
    output_code = []
    encoder_code = []
    did_output = {}
    for msgname, msg in encoders:
        msgid = msg_to_id[msg]
        if msgid in did_output:
            continue
        s = msg
        did_output[msgid] = True
        code = ('    if (__builtin_strcmp(str, "%s") == 0)\n'
                '        return &command_encoder_%s;\n' % (s, msgid))
        if msgname is None:
            parser = msgproto.OutputFormat(msgid, msg)
            output_code.append(code)
        else:
            parser = msgproto.MessageFormat(msgid, msg)
            encoder_code.append(code)
        parsercode = build_parser(parser, 0, all_param_types)
        encoder_defs.append(
            "const struct command_encoder command_encoder_%s PROGMEM = {"
            "    %s\n};\n" % (
                msgid, parsercode))
    fmt = """
%s

const __always_inline struct command_encoder *
ctr_lookup_encoder(const char *str)
{
    %s
    return NULL;
}

const __always_inline struct command_encoder *
ctr_lookup_output(const char *str)
{
    %s
    return NULL;
}
"""
    return fmt % ("".join(encoder_defs).strip(), "".join(encoder_code).strip(),
                  "".join(output_code).strip())

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
    externs = {}
    for msgid in range(max_cmd_msgid+1):
        if msgid not in cmd_by_id:
            index.append(" {\n},")
            continue
        funcname, flags, msgname = cmd_by_id[msgid]
        msg = messages_by_name[msgname]
        externs[funcname] = 1
        parser = msgproto.MessageFormat(msgid, msg)
        parsercode = build_parser(parser, 1, all_param_types)
        index.append(" {%s\n    .flags=%s,\n    .func=%s\n}," % (
            parsercode, flags, funcname))
    index = "".join(index).strip()
    externs = "\n".join(["extern void "+funcname+"(uint32_t*);"
                         for funcname in sorted(externs)])
    fmt = """
%s

const struct command_parser command_index[] PROGMEM = {
%s
};

const uint8_t command_index_size PROGMEM = ARRAY_SIZE(command_index);
"""
    return fmt % (externs, index)


######################################################################
# Identify data dictionary generation
######################################################################

def build_identify(cmd_by_id, msg_to_id, responses, version, toolstr):
    #commands, messages
    messages = dict((msgid, msg) for msg, msgid in msg_to_id.items())
    data = {}
    for h in Handlers:
        h.update_data_dictionary(data)
    data['messages'] = messages
    data['commands'] = sorted(cmd_by_id.keys())
    data['responses'] = sorted(responses)
    data['version'] = version
    data['build_versions'] = toolstr

    # Format compressed info into C code
    data = json.dumps(data)
    zdata = zlib.compress(data, 9)
    out = []
    for i in range(len(zdata)):
        if i % 8 == 0:
            out.append('\n   ')
        out.append(" 0x%02x," % (ord(zdata[i]),))
    fmt = """
// version: %s
// build_versions: %s

const uint8_t command_identify_data[] PROGMEM = {%s
};

// Identify size = %d (%d uncompressed)
const uint32_t command_identify_size PROGMEM
    = ARRAY_SIZE(command_identify_data);
"""
    return data, fmt % (version, toolstr, ''.join(out), len(zdata), len(data))


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
    ver = check_output("git describe --always --tags --long --dirty").strip()
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

# Run "tool --version" for each specified tool and extract versions
def tool_versions(tools):
    tools = [t.strip() for t in tools.split(';')]
    versions = ['', '']
    success = 0
    for tool in tools:
        # Extract first line from "tool --version" output
        verstr = check_output("%s --version" % (tool,)).split('\n')[0]
        # Check if this tool looks like a binutils program
        isbinutils = 0
        if verstr.startswith('GNU '):
            isbinutils = 1
            verstr = verstr[4:]
        # Extract version information and exclude program name
        if ' ' not in verstr:
            continue
        prog, ver = verstr.split(' ', 1)
        if not prog or not ver:
            continue
        # Check for any version conflicts
        if versions[isbinutils] and versions[isbinutils] != ver:
            logging.debug("Mixed version %s vs %s" % (
                repr(versions[isbinutils]), repr(ver)))
            versions[isbinutils] = "mixed"
            continue
        versions[isbinutils] = ver
        success += 1
    cleanbuild = versions[0] and versions[1] and success == len(tools)
    return cleanbuild, "gcc: %s binutils: %s" % (versions[0], versions[1])


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
    opts.add_option("-t", "--tools", dest="tools", default="",
                    help="list of build programs to extract version from")
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
    encoders = []
    # Parse request file
    ctr_dispatch = { k: v for h in Handlers for k, v in h.ctr_dispatch.items() }
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
        if cmd in ctr_dispatch:
            ctr_dispatch[cmd](req)
        elif cmd == '_DECL_COMMAND':
            funcname, flags, msgname = parts[1:4]
            if msgname in commands:
                error("Multiple definitions for command '%s'" % msgname)
            commands[msgname] = (funcname, flags, msgname)
            msg = req.split(None, 3)[3]
            m = messages_by_name.get(msgname)
            if m is not None and m != msg:
                error("Conflicting definition for command '%s'" % msgname)
            messages_by_name[msgname] = msg
        elif cmd == '_DECL_ENCODER':
            msgname = parts[1]
            m = messages_by_name.get(msgname)
            if m is not None and m != msg:
                error("Conflicting definition for message '%s'" % msgname)
            messages_by_name[msgname] = msg
            encoders.append((msgname, msg))
        elif cmd == '_DECL_OUTPUT':
            encoders.append((None, msg))
        else:
            error("Unknown build time command '%s'" % cmd)
    # Create unique ids for each message type
    msgid = max(msgproto.DefaultMessages.keys())
    msg_to_id = dict((m, i) for i, m in msgproto.DefaultMessages.items())
    for msgname in commands.keys() + [m for n, m in encoders]:
        msg = messages_by_name.get(msgname, msgname)
        if msg not in msg_to_id:
            msgid += 1
            msg_to_id[msg] = msgid
    # Create message definitions
    all_param_types = {}
    parsercode = build_encoders(encoders, msg_to_id, all_param_types)
    # Create command definitions
    cmd_by_id = dict((msg_to_id[messages_by_name.get(msgname, msgname)], cmd)
                     for msgname, cmd in commands.items())
    cmdcode = build_commands(cmd_by_id, messages_by_name, all_param_types)
    paramcode = build_param_types(all_param_types)
    # Create identify information
    cleanbuild, toolstr = tool_versions(options.tools)
    version = build_version(options.extra)
    sys.stdout.write("Version: %s\n" % (version,))
    responses = [msg_to_id[msg] for msgname, msg in messages_by_name.items()
                 if msgname not in commands]
    datadict, icode = build_identify(
        cmd_by_id, msg_to_id, responses, version, toolstr)
    # Write output
    f = open(outcfile, 'wb')
    f.write(FILEHEADER + "".join([h.generate_code() for h in Handlers])
            + paramcode + parsercode + cmdcode + icode)
    f.close()

    # Write data dictionary
    if options.write_dictionary:
        f = open(options.write_dictionary, 'wb')
        f.write(datadict)
        f.close()

if __name__ == '__main__':
    main()
