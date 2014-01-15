#!/usr/bin/env python
import sys
import errno
import argparse
import re
import math
from itertools import ifilter
from collections import defaultdict
from operator import attrgetter

from osv import trace, debug, prof

class symbol_printer:
    def __init__(self, resolver, formatter):
        self.resolver = resolver
        self.formatter = formatter

    def __call__(self, addr):
        return self.formatter(self.resolver(addr))

class src_addr_formatter:
    def __init__(self, args):
        self.args = args

    def __call__(self, src_addr):
        if not src_addr.name:
            return '0x%x' % src_addr.addr

        text = src_addr.name

        if self.args.show_file_name:
            text += ' %s' % src_addr.filename

        if self.args.show_line_number:
            text += ':%s' % src_addr.line

        if self.args.show_address:
            text += ' @ 0x%x' % src_addr.addr

        return text

def add_trace_source_options(parser):
    parser.add_argument("tracefile", help="Path to trace file")

def get_trace_reader(args):
    return trace.read_file(args.tracefile)

def add_symbol_resolution_options(parser):
    group = parser.add_argument_group('symbol resolution')
    group.add_argument("-d", "--debug", action="store_true", help="use loader.elf from debug build")
    group.add_argument("-e", "--exe", action="store", help="path to the object file used for symbol resolution")
    group.add_argument("-x", "--no-resolve", action='store_true', help="do not resolve symbols")
    group.add_argument("-L", "--show-line-number", action='store_true', help="show line numbers")
    group.add_argument("-A", "--show-address", action='store_true', help="show raw addresses")
    group.add_argument("-F", "--show-file-name", action='store_true', help="show file names")

class BeautifyingResolver(object):
    def __init__(self, delegate):
        self.delegate = delegate

    def __call__(self, addr):
        src_addr = self.delegate(addr)
        if src_addr.name:
            if src_addr.name.startswith("void sched::thread::do_wait_until<"):
                src_addr.name = "sched::thread::do_wait_until"
            elif src_addr.name.startswith("do_wait_until<"):
                src_addr.name = "do_wait_until"
        return src_addr

def symbol_resolver(args):
    if args.no_resolve:
        return debug.DummyResolver()

    if args.exe:
        elf_path = args.exe
    elif args.debug:
        elf_path = 'build/debug/loader.elf'
    else:
        elf_path = 'build/release/loader.elf'

    return BeautifyingResolver(debug.SymbolResolver(elf_path))

def get_backtrace_formatter(args):
    if not args.backtrace:
        return lambda backtrace: ''

    return trace.BacktraceFormatter(
        symbol_printer(symbol_resolver(args), src_addr_formatter(args)))

def list_trace(args):
    backtrace_formatter = get_backtrace_formatter(args)
    with get_trace_reader(args) as reader:
        for t in reader.get_traces():
            print t.format(backtrace_formatter)

def add_profile_options(parser):
    group = parser.add_argument_group('profile options')
    group.add_argument("-r", "--caller-oriented", action='store_true', help="change orientation to caller-based; reverses order of frames")
    group.add_argument("-m", "--merge-threads", action='store_true', help="show one merged tree for all threads")
    group.add_argument("--function", action='store', help="use given function as tree root")
    group.add_argument("--since", action='store', help="show profile since this timestamp [ns]")
    group.add_argument("--until", action='store', help="show profile until this timestamp [ns]")
    group.add_argument("--min-duration", action='store', help="show only nodes with resident time not shorter than this, eg: 200ms")
    group.add_argument("--max-levels", action='store', help="maximum number of tree levels to show")

def get_wait_profile(traces):
    return prof.get_duration_profile(traces, "sched_wait", "sched_wait_ret")

def int_or_none(value):
    if value:
        return int(value)
    return None

def show_profile(args, sample_producer):
    resolver = symbol_resolver(args)
    time_range = prof.TimeRange(int_or_none(args.since), int_or_none(args.until))

    if args.min_duration:
        min_duration = prof.parse_time_as_nanos(args.min_duration)
        node_filter = lambda node: node.resident_time >= min_duration
    else:
        node_filter = None

    with get_trace_reader(args) as reader:
        prof.print_profile(sample_producer(reader.get_traces()),
            symbol_resolver=resolver,
            caller_oriented=args.caller_oriented,
            merge_threads=args.merge_threads,
            src_addr_formatter=src_addr_formatter(args),
            root_function=args.function,
            node_filter=node_filter,
            time_range=time_range,
            max_levels=int_or_none(args.max_levels))

def prof_wait(args):
    show_profile(args, get_wait_profile)

def prof_hit(args):
    show_profile(args, lambda traces: prof.get_hit_profile(traces, args.tracepoint))

def print_summary(args, printer=sys.stdout.write):
    count_per_tp = defaultdict(lambda: 0)
    count = 0
    min_time = None
    max_time = None

    with get_trace_reader(args) as reader:
        for t in reader.get_traces():
            count += 1
            count_per_tp[t.tp] += 1

            if not min_time:
                min_time = t.time
            else:
                min_time = min(min_time, t.time)

            max_time = max(max_time, t.time)

    if count == 0:
        print "No samples"
        return

    print "Collected %d samples spanning %s" % (count, prof.format_time(max_time - min_time))

    max_name_len = reduce(max, map(lambda tp: len(tp.name), count_per_tp.iterkeys()))
    format = "  %%-%ds %%8s" % (max_name_len)
    print "\nTracepoint statistics:\n"
    print format % ("name", "count")
    print format % ("----", "-----")

    for tp, count in sorted(count_per_tp.iteritems(), key=lambda (tp, count): tp.name):
        print format % (tp.name, count)

    print

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="trace file processing")
    subparsers = parser.add_subparsers(help="Command")

    cmd_list = subparsers.add_parser("list", help="list trace")
    add_symbol_resolution_options(cmd_list)
    cmd_list.add_argument("-b", "--backtrace", action="store_true", help="show backtrace")
    add_trace_source_options(cmd_list)
    cmd_list.set_defaults(func=list_trace)

    cmd_summary = subparsers.add_parser("summary", help="list timed traces", description="""
        Prints block samples along with their duration in seconds with nanosecond precision. The duration
        is calculated bu subtracting timestamps between entry sample and the matched ending sample.
        The convention is that the ending sample has the same name as the entry sample plus '_ret' or '_err' suffix.
        """)
    add_trace_source_options(cmd_summary)
    cmd_summary.set_defaults(func=print_summary)

    cmd_prof_wait = subparsers.add_parser("prof-wait", help="show wait profile", description="""
        Prints profile showing amount of time spent inside sched::thread::wait(). Among other
        things this includes time a thread was blocked on a mutex or condvar.
        Requires sched_wait and sched_wait_ret tracepoints.
        Requires trace samples with backtrace.
        """)
    add_symbol_resolution_options(cmd_prof_wait)
    add_trace_source_options(cmd_prof_wait)
    add_profile_options(cmd_prof_wait)
    cmd_prof_wait.set_defaults(func=prof_wait)

    cmd_prof_hit = subparsers.add_parser("prof", help="show trace hit profile", description="""
        Prints profile showing number of times given tracepoint was reached.
        Requires trace samples with backtrace.
        """)
    add_symbol_resolution_options(cmd_prof_hit)
    add_trace_source_options(cmd_prof_hit)
    add_profile_options(cmd_prof_hit)
    cmd_prof_hit.add_argument("-t", "--tracepoint", action="store", help="name of the tracepint to count")
    cmd_prof_hit.set_defaults(func=prof_hit)

    args = parser.parse_args()

    try:
        args.func(args)
    except IOError as e:
        if e.errno != errno.EPIPE:
            raise