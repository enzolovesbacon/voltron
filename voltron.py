#!/usr/bin/env python

from __future__ import print_function

import os
import sys
import socket
import asyncore
import threading
import argparse
import logging
import logging.config
import Queue
import termcolor

try:
    import cPickle as pickle
except:
    import pickle

try:
    import gdb
    in_gdb = True
except:
    in_gdb = False

try:
    import pygments
    import pygments.lexers
    import pygments.formatters
    have_pygments = True
except:
    have_pygments = False

SOCK = "/tmp/voltron.sock"
READ_MAX = 0xFFFF
LOG_CONFIG = {
        'version': 1,
        'formatters': {
            'standard': {'format': '[%(levelname)s]: %(message)s'}
        },
        'handlers': {
            'default': {
                'class': 'logging.StreamHandler',
                'formatter': 'standard'
            }
        },
        'loggers': {
            '': {
                'handlers': ['default'],
                'level': 'INFO',
                'propogate': True,
            }
        }
}

ADDR_FORMAT_64 = '0x{0:0=16X}'
ADDR_FORMAT_32 = '0x{0:0=8X}'
ADDR_FORMAT_16 = '0x{0:0=4X}'
SEGM_FORMAT_16 = '{0:0=4X}'

COLOURS = {
    'label': 'green',
    'value': 'grey',
    'modified': 'red',
    'header': 'blue',
    'flags': 'red',
}

DISASM_MAX = 32
STACK_MAX = 64

log = None
clients = []
queue = None

def main():
    global log, queue

    # Configure logging
    logging.config.dictConfig(LOG_CONFIG)
    log = logging.getLogger('')

    # Set up queue
    queue = Queue.Queue()

    if in_gdb:
        # Load GDB command
        log.debug('Loading GDB command')
        VoltronCommand()
    else:
        # Parse command line args
        parser = argparse.ArgumentParser()
        parser.add_argument('--debug', '-d', action='store_true', help='print debug logging')
        subparsers = parser.add_subparsers(title='subcommands', description='valid subcommands', help='additional help')
        sp = subparsers.add_parser('reg', description='register view')
        sp.set_defaults(func=RegisterView)
        g = sp.add_mutually_exclusive_group()
        g.add_argument('--horizontal', '-o', action='store_true', help='horizontal orientation (default)', default=False)
        g.add_argument('--vertical', '-v', action='store_true', help='vertical orientation', default=True)
        sp = subparsers.add_parser('stack', description='stack view')
        sp.set_defaults(func=StackView)
        sp = subparsers.add_parser('disasm', description='disassembly view')
        sp.set_defaults(func=DisasmView)
        sp = subparsers.add_parser('bt', description='backtrace view')
        sp.set_defaults(func=BacktraceView)
        args = parser.parse_args()

        if args.debug:
            log.setLevel(logging.DEBUG)

        # Run the appropriate view
        try:
            view = args.func(args)
            view.run()
            view.cleanup()
        except Exception as e:
            log.error(e)


#
# Server side code
#

# This is the actual GDB command. Should be able to just add an LLDB version I guess.
if in_gdb:
    class VoltronCommand (gdb.Command):
        def __init__(self):
            super(VoltronCommand, self).__init__("voltron", gdb.COMMAND_NONE, gdb.COMPLETE_NONE)
            self.running = False

        def invoke(self, arg, from_tty):
            global log
            if arg.startswith("start"):
                if 'debug' in arg:
                    log.setLevel(logging.DEBUG)
                self.start()
            elif arg == "stop":
                self.stop()
            elif arg == "status":
                self.status()
            else:
                print("Usage: voltron <start|stop|status>")

        def start(self):
            if not self.running:
                print("Starting voltron")
                self.running = True
                gdb.events.stop.connect(self.stop_handler)
                self.thread = ServerThread()
                self.thread.start()
            else:
                print("Already running")

        def stop(self):
            if self.running:
                print("Stopping voltron")
                gdb.events.stop.disconnect(self.stop_handler)
                self.thread.set_should_exit(True)
                self.thread.join(10)
                if self.thread.isAlive():
                    print("Failed to stop voltron :<")
                self.running = False
            else:
                print("Not running")

        def status(self):
            if self.running:
                print("There are {} clients attached".format(len(clients)))
                for client in clients:
                    print("{} registered for: {}".format(client, client.registration['for_types']))
            else:
                print("Not running")

        def get_registers(self):
            regs = ['rax','rbx','rcx','rdx','rbp','rsp','rdi','rsi','rip','r8','r9','r10','r11','r12','r13','r14','r15','cs','ds','es','fs','gs','ss']
            vals = {}
            for reg in regs:
                try:
                    vals[reg] = int(gdb.parse_and_eval('(long long)$'+reg)) & 0xFFFFFFFFFFFFFFFF
                except:
                    log.debug('Failed getting reg: ' + reg)
                    vals[reg] = 'N/A'
            vals['eflags'] = str(gdb.parse_and_eval('$eflags'))
            log.debug('Got registers: ' + str(vals))
            return vals

        def get_disasm(self):
            res = gdb.execute('x/{}i $rip'.format(DISASM_MAX), to_string=True)
            return res

        def get_stack(self):
            rsp = int(gdb.parse_and_eval('(long long)$rsp')) & 0xFFFFFFFFFFFFFFFF
            res = str(gdb.selected_inferior().read_memory(rsp, STACK_MAX*16))
            return res

        def get_backtrace(self):
            res = gdb.execute('bt', to_string=True)
            return res

        def stop_handler(self, event):
            log.debug('Stop handler')

            # Get registers
            log.debug('Getting registers')
            regs = self.get_registers()
            event = {'msg_type': 'update', 'update_type': 'register', 'data': regs}
            queue.put(event)

            # Disassemble code at PC
            log.debug('Getting disasm')
            disasm = self.get_disasm()
            event = {'msg_type': 'update', 'update_type': 'disasm', 'data': disasm}
            queue.put(event)

            # Get a stack snippet
            log.debug('Getting stack')
            stack = self.get_stack()
            event = {'msg_type': 'update', 'update_type': 'stack', 'data': {'data': stack, 'sp': regs['rsp'] } }
            queue.put(event)

            # Get a backtrace
            log.debug('Getting backtrace')
            bt = self.get_backtrace()
            event = {'msg_type': 'update', 'update_type': 'bt', 'data': bt }
            queue.put(event)



# Socket for talking to an individual client
class ClientHandler (asyncore.dispatcher):
    def handle_read(self):
        data = self.recv(READ_MAX)
        if data.strip() != "":
            try:
                log.debug('Received msg: ' + data)
                msg = pickle.loads(data)
            except:
                log.error('Invalid message data: ' + data)

            if msg['msg_type'] == 'register':
                self.handle_register(msg)
            else:
                log.error('Invalid message type: ' + msg['msg_type'])

    def handle_register(self, msg):
        log.debug('Registering client {} for types: {}'.format(self, msg['for_types']))
        self.registration = msg

    def send_event(self, event):
        log.debug('Sending event to client {}: {}'.format(self, event))
        self.send(pickle.dumps(event))


# Main server socket for accept()s
class Server (asyncore.dispatcher):
    def __init__(self, sockfile):
        asyncore.dispatcher.__init__(self)
        try:
            os.remove(SOCK)
        except:
            pass
        self.create_socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.bind(sockfile)
        self.listen(1)

    def handle_accept(self):
        global clients
        pair = self.accept()
        if pair is not None:
            sock, addr = pair
            client = ClientHandler(sock)
            log.debug('Received connection: ' + str(client))
            clients.append(client)


# Thread spun off when the plugin is started to listen for incoming client connections, and send out any
# events that have been queued by the hooks in the debugger command class
class ServerThread (threading.Thread):
    def run(self):
        global clients, queue

        # Create a server instance
        serv = Server(SOCK)
        self.lock = threading.Lock()
        self.set_should_exit(False)

        # Main event loop
        while not self.should_exit():
            # Check sockets for activity
            asyncore.loop(count=1, timeout=0.1)

            # Process any events in the queue
            while not queue.empty():
                event = queue.get()
                for client in clients:
                    if event['msg_type'] == 'update' and event['update_type'] in client.registration['for_types']:
                        client.send_event(event)

        # Clean up
        serv.close()


    def should_exit(self):
        self.lock.acquire()
        r = self._should_exit
        self.lock.release()
        return r

    def set_should_exit(self, should_exit):
        self.lock.acquire()
        self._should_exit = should_exit
        self.lock.release()


#
# Client-side code
#

# Socket to register with the server and receive messages, calls view's render() method when a message comes in
class Client (asyncore.dispatcher):
    def __init__(self, view=None, types=None):
        asyncore.dispatcher.__init__(self)
        self.view = view
        self.types = types
        self.create_socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.connect(SOCK)

    def register(self):
        log.debug('Client {} registering for types: {}'.format(self, self.types))
        msg = {'msg_type': 'register', 'for_types': self.types}
        log.debug('Sending: ' + str(msg))
        self.send(pickle.dumps(msg))

    def handle_read(self):
        data = self.recv(READ_MAX)
        try:
            msg = pickle.loads(data)
            log.debug('Received message: ' + str(msg))
        except:
            log.debug('Invalid message: ' + data)
        if self.view:
            self.view.render(msg)


# Parent class for all views
class VoltronView (object):
    def __init__(self, args=None):
        log.debug('Loading view: ' + self.__class__.__name__)
        self.client = None
        self.types = None
        self.args = args
        os.system('tput civis')

    def cleanup(self):
        log.debug('Cleaning up view')
        os.system('tput cnorm')

    def connect(self):
        try:
            self.client = Client(view=self, types=self.types)
            self.client.register()
        except Exception as e:
            log.error('Exception connecting: ' + str(e))
            raise e

    def run(self):
        os.system('clear')
        log.info('Waiting for an update from the debugger')
        asyncore.loop()

    def render(self, msg=None):
        log.warning('Might wanna implement render() in this view eh')

    def clear(self):
        # Clear the window - this sucks, should probably do it with ncurses at some stage
        os.system('clear')

    def window_size(self):
        # Get terminal size - this also sucks, but curses sucks more
        height, width = os.popen('stty size').read().split()
        height = int(height)
        width = int(width)
        return (height, width)

    def hexdump(self, src, length=16, sep='.', offset=0):
        FILTER = ''.join([(len(repr(chr(x))) == 3) and chr(x) or sep for x in range(256)])
        lines = []
        for c in xrange(0, len(src), length):
            chars = src[c:c+length]
            hex = ' '.join(["%02X" % ord(x) for x in chars])
            if len(hex) > 24:
                hex = "%s %s" % (hex[:24], hex[24:])
            printable = ''.join(["%s" % ((ord(x) <= 127 and FILTER[ord(x)]) or sep) for x in chars])
            lines.append("%s:  %-*s  |%s|\n" % (ADDR_FORMAT_64.format(offset+c), length*3, hex, printable))
        return ''.join(lines).strip()

    def format_header(self, title=None, left=None, right=None):
        height, width = self.window_size()

        # Left data
        header = ''
        if left != None:
            header = left

        # Dashes
        dashlen = width - len(header) - len(title)
        if dashlen < 0:
            dashlen = 1
        header += '-' * dashlen

        # Title
        header = termcolor.colored(header, COLOURS['header']) + termcolor.colored(title, 'white', attrs=['bold'])
        
        return header

    def format_footer(self, title=None, left=None, right=None):
        height, width = self.window_size()
        dashlen = width
        footer = '-' * dashlen
        footer = termcolor.colored(footer, COLOURS['header']) 
        return footer


# Class to actually render the view
class RegisterView (VoltronView):
    FORMAT_INFO = {
        'x64': [
            {
                'regs':             ['rax','rbx','rcx','rdx','rbp','rsp','rdi','rsi','rip','r8','r9','r10','r11', 'r12','r13','r14','r15'],
                'label_format':     '{0:3s}:',
                'label_mod':        str.upper,
                'label_colour':     COLOURS['label'],
                'value_format':     ADDR_FORMAT_64,
                'value_mod':        None,
                'value_colour':     COLOURS['value'],
                'value_colour_mod': COLOURS['modified']
            },
            {
                'regs':             ['cs','ds','es','fs','gs','ss'],
                'label_format':     '{0}:',
                'label_mod':        str.upper,
                'label_colour':     COLOURS['label'],
                'value_format':     SEGM_FORMAT_16,
                'value_mod':        None,
                'value_colour':     COLOURS['value'],
                'value_colour_mod': COLOURS['modified']
            },
            {
                'regs':             ['eflags'],
                'value_format':     '{0}',
                'value_mod':        None
            },
        ]
    }
    FORMAT_DEFAULTS = {
        'label_format':     '{}:',
        'label_mod':        str.upper,
        'label_colour':     COLOURS['label'],
        'value_format':     ADDR_FORMAT_64,
        'value_mod':        None,
        'value_colour':     COLOURS['value'],
        'value_colour_mod': COLOURS['modified']
    }
    TEMPLATE_H = (
        "{raxl} {rax}  {rbxl} {rbx}  {rbpl} {rbp}  {rspl} {rsp}  {eflags}\n"
        "{rdil} {rdi}  {rsil} {rsi}  {rdxl} {rdx}  {rcxl} {rcx}  {ripl} {rip}\n"
        "{r8l} {r8}  {r9l} {r9}  {r10l} {r10}  {r11l} {r11}  {r12l} {r12}\n"
        "{r13l} {r13}  {r14l} {r14}  {r15l} {r15}\n"
        "{csl} {cs}  {dsl} {ds}  {esl} {es}  {fsl} {fs}  {gsl} {gs}  {ssl} {ss}\n"
    )
    TEMPLATE_V = (
        "{ripl} {rip}\n\n"
        "{raxl} {rax}\n{rbxl} {rbx}\n{rbpl} {rbp}\n{rspl} {rsp}\n"
        "{rdil} {rdi}\n{rsil} {rsi}\n{rdxl} {rdx}\n{rcxl} {rcx}\n"
        "{r8l} {r8}\n{r9l} {r9}\n{r10l} {r10}\n{r11l} {r11}\n{r12l} {r12}\n"
        "{r13l} {r13}\n{r14l} {r14}\n{r15l} {r15}\n"
        "{csl}  {cs}  {dsl}  {ds}\n{esl}  {es}  {fsl}  {fs}\n{gsl}  {gs}  {ssl}  {ss}\n"
        "    {eflags}\n"
    )

    last_regs = None

    def __init__(self, args=None):
        super(RegisterView, self).__init__(args)
        self.types = ['register']
        self.connect()

    def render(self, msg=None):
        self.clear()

        # Grab the appropriate template
        template = self.TEMPLATE_V if self.args.vertical else self.TEMPLATE_H

        # Process formatting settings
        data = msg['data']
        formats = self.FORMAT_INFO['x64']
        formatted = {}
        for fmt in formats:
            # Apply defaults where they're missing
            fmt = dict(self.FORMAT_DEFAULTS.items() + fmt.items())

            # Format the data for each register
            for reg in fmt['regs']:
                # Format the label
                label = fmt['label_format'].format(reg)
                if fmt['label_mod'] != None:
                    label = fmt['label_mod'](label)
                formatted[reg+'l'] =  termcolor.colored(label, fmt['label_colour'])

                # Format the value
                val = data[reg]
                if type(val) == str:
                    formatted[reg] = termcolor.colored(val, fmt['value_colour'])
                else:
                    colour = fmt['value_colour']
                    if self.last_regs == None or self.last_regs != None and val != self.last_regs[reg]:
                        colour = fmt['value_colour_mod']
                    val = fmt['value_format'].format(val)
                    if fmt['value_mod'] != None:
                        val = fmt['value_mod'](val)
                    formatted[reg] = termcolor.colored(val, colour)

        log.debug('Formatted: ' + str(formatted))
        print(self.format_header('[regs]'))
        print(template.format(**formatted), end='')
        print(self.format_footer(), end='')
        sys.stdout.flush()

        # Store the regs
        self.last_regs = data


class DisasmView (VoltronView):
    DISASM_SHOW_LINES = 16
    DISASM_SEP_WIDTH = 90

    def __init__(self, args=None):
        super(DisasmView, self).__init__(args)
        self.types = ['disasm']
        self.connect()

    def render(self, msg=None):
        self.clear()
        height, width = self.window_size()

        # Get the disasm
        disasm = msg['data']
        disasm = '\n'.join(disasm.split('\n')[:height-2])

        # Pygmentize output
        if have_pygments:
            try:
                lexer = pygments.lexers.get_lexer_by_name('gdb')
                disasm = pygments.highlight(disasm, lexer, pygments.formatters.Terminal256Formatter())
            except Exception as e:
                log.warning('Failed to highlight disasm: ' + str(e))

        # Print output
        print(self.format_header('[code]'))
        print(disasm.rstrip())
        print(self.format_footer(), end='')
        sys.stdout.flush()


class StackView (VoltronView):
    STACK_SHOW_LINES = 16
    STACK_SEP_WIDTH = 90

    def __init__(self, args=None):
        super(StackView, self).__init__(args)
        self.types = ['stack']
        self.connect()

    def render(self, msg=None):
        self.clear()
        height, width = self.window_size()

        # Get the stack data
        data = msg['data']
        stack_raw = data['data']
        sp = data['sp']
        stack_raw = stack_raw[:(height-2)*16]

        # Hexdump it
        lines = self.hexdump(stack_raw, offset=sp).split('\n')
        lines.reverse()
        stack = '\n'.join(lines)

        # Print output
        sp_addr = '[0x{0:0=4x}:'.format(len(stack_raw)) + ADDR_FORMAT_64.format(sp) + ']'
        print(self.format_header('[stack]', left=sp_addr))
        print(stack.strip())
        print(self.format_footer(), end='')
        sys.stdout.flush()


class BacktraceView (VoltronView):
    def __init__(self, args=None):
        super(BacktraceView, self).__init__(args)
        self.types = ['bt']
        self.connect()

    def render(self, msg=None):
        self.clear()
        height, width = self.window_size()

        # Get the back trace data
        data = msg['data']
        lines = data.split('\n')
        pad = height - len(lines) - 2
        if pad < 0:
            pad = 0

        # Print output
        print(self.format_header('[backtrace]'))
        print(data.strip())
        if pad > 0:
            print('\n' * pad) 
        print(self.format_footer(), end='')
        sys.stdout.flush()
        

if __name__ == "__main__":
    main()