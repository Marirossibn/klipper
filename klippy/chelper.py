# Wrapper around C helper code
#
# Copyright (C) 2016  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import os, logging
import cffi

COMPILE_CMD = "gcc -Wall -g -O -shared -fPIC -o %s %s"
SOURCE_FILES = ['stepcompress.c', 'serialqueue.c']
DEST_LIB = "c_helper.so"
OTHER_FILES = ['list.h', 'serialqueue.h']

defs_stepcompress = """
    struct stepcompress *stepcompress_alloc(uint32_t max_error
        , uint32_t queue_step_msgid, uint32_t oid);
    void stepcompress_push(struct stepcompress *sc, double step_clock);
    double stepcompress_push_factor(struct stepcompress *sc
        , double steps, double step_offset
        , double clock_offset, double factor);
    double stepcompress_push_sqrt(struct stepcompress *sc
        , double steps, double step_offset
        , double clock_offset, double sqrt_offset, double factor);
    void stepcompress_reset(struct stepcompress *sc, uint64_t last_step_clock);
    void stepcompress_queue_msg(struct stepcompress *sc
        , uint32_t *data, int len);
    uint32_t stepcompress_get_errors(struct stepcompress *sc);

    struct steppersync *steppersync_alloc(struct serialqueue *sq
        , struct stepcompress **sc_list, int sc_num, int move_num);
    void steppersync_flush(struct steppersync *ss, uint64_t move_clock);
"""

defs_serialqueue = """
    #define MESSAGE_MAX 64
    struct pull_queue_message {
        uint8_t msg[MESSAGE_MAX];
        int len;
        double sent_time, receive_time;
    };

    struct serialqueue *serialqueue_alloc(int serial_fd, int write_only);
    void serialqueue_exit(struct serialqueue *sq);
    struct command_queue *serialqueue_alloc_commandqueue(void);
    void serialqueue_send(struct serialqueue *sq, struct command_queue *cq
        , uint8_t *msg, int len, uint64_t min_clock, uint64_t req_clock);
    void serialqueue_encode_and_send(struct serialqueue *sq
        , struct command_queue *cq, uint32_t *data, int len
        , uint64_t min_clock, uint64_t req_clock);
    void serialqueue_pull(struct serialqueue *sq, struct pull_queue_message *pqm);
    void serialqueue_set_baud_adjust(struct serialqueue *sq, double baud_adjust);
    void serialqueue_set_clock_est(struct serialqueue *sq, double est_clock
        , double last_ack_time, uint64_t last_ack_clock);
    void serialqueue_flush_ready(struct serialqueue *sq);
    void serialqueue_get_stats(struct serialqueue *sq, char *buf, int len);
    int serialqueue_extract_old(struct serialqueue *sq, int sentq
        , struct pull_queue_message *q, int max);
"""

# Return the list of file modification times
def get_mtimes(srcdir, filelist):
    out = []
    for filename in filelist:
        pathname = os.path.join(srcdir, filename)
        try:
            t = os.path.getmtime(pathname)
        except os.error:
            continue
        out.append(t)
    return out

# Check if the code needs to be compiled
def check_build_code(srcdir):
    src_times = get_mtimes(srcdir, SOURCE_FILES + OTHER_FILES)
    obj_times = get_mtimes(srcdir, [DEST_LIB])
    if not obj_times or max(src_times) > min(obj_times):
        logging.info("Building C code module")
        srcfiles = [os.path.join(srcdir, fname) for fname in SOURCE_FILES]
        destlib = os.path.join(srcdir, DEST_LIB)
        os.system(COMPILE_CMD % (destlib, ' '.join(srcfiles)))

FFI_main = None
FFI_lib = None

# Return the Foreign Function Interface api to the caller
def get_ffi():
    global FFI_main, FFI_lib
    if FFI_lib is None:
        srcdir = os.path.dirname(os.path.realpath(__file__))
        check_build_code(srcdir)
        FFI_main = cffi.FFI()
        FFI_main.cdef(defs_stepcompress)
        FFI_main.cdef(defs_serialqueue)
        FFI_lib = FFI_main.dlopen(os.path.join(srcdir, DEST_LIB))
    return FFI_main, FFI_lib
