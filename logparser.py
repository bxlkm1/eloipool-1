import parse
import json
import argparse
import sys
from collections import OrderedDict as odict


parser = argparse.ArgumentParser()
parser.add_argument('logFile', help='log filename')
parser.add_argument('-o', '--output', help='Raw output log operations (CSV format)')
parser.add_argument('-s', '--summary', action='store_true', help='Summary of operations')
parser.add_argument('--max-notify-delta', type=int, default=500, help='Max delta between notify calls (in milliseconds)')
args = parser.parse_args()


def delta_ms(start, finish):
    delta = finish - start
    return delta.seconds * 1000.0 + delta.microseconds / 1000.0

class LogFile:

    def __init__(self, filename, output, summary, logtype):
        self.filename = filename
        self.output = open(output, "w+") if output else None
        self.summary = summary
        self.getblocktemplates = odict()
        self.server_calls = {}      # calls to bitcoind / rskd
        self.client_calls = {}      # calls to clients
        self.client_jobs = {}       # jobs received from clients
        self.submit_jobs = {}       # jobs sent to bitcoind
        self.logtype = logtype

    def parse(self):
        """Parse log file"""
        with open(self.filename, "r") as f:
            # A logged line can contain a '\n', so we join them before parsing
            prevline = ""
            for line in f:
                if (self.logtype == 'ckpool' and line[0] != "[") or \
                    (self.logtype == 'eloipool' and line[0] != '2'):
                    prevline += line
                else:
                    if prevline:
                        self.parseline(prevline)
                    prevline = line
            if prevline:
                self.parseline(prevline)
            self.flush_info()

    def parseline(self, line):
        pos = line.find("ROOTSTOCK:")
        if pos < 0: # Ignore lines that were not logged by us
            return

        # Log line format is time\tModule\tLevel\tROOTSTOCK: <operation>: <data>
        result = parse.parse("{}\t{}\t{}\t{}: {}: {}", line)
        if result is None or len(result.fixed) != 6 or result.fixed[3] != 'ROOTSTOCK':
            # Drop ill formed lines
            print("Error: Failed to parse: |{}|".format(line), file=sys.stderr)
            return
        time = parse.parse("{:ti}", result.fixed[0].replace(",", ".")).fixed[0]
        operation = result.fixed[4]
        data = result.fixed[5]

        # Interpret logged operations
        if operation == 'json_rpc_call':
            rpc_call = parse.parse("{:x}, {}", data)
            if (rpc_call.fixed is None) or (len(rpc_call.fixed) != 2):
                print("Error: Failed to parse: |{}|".format(line), file=sys.stderr)
                return None
            call_id = rpc_call.fixed[0]
            call = rpc_call.fixed[1]

            self.process_operation('json_rpc_call', call_id, time, call)
            return

        elif operation == 'json_rpc_reply':
            rpc_reply = parse.parse("{:x}, {}", data)
            if (rpc_reply.fixed is None) or (len(rpc_reply.fixed) != 2):
                print("Error: Failed to parse: |{}|".format(line), file=sys.stderr)
                return None
            call_id = rpc_reply.fixed[0]
            reply = rpc_reply.fixed[1]

            self.process_operation('json_rpc_reply', call_id, time, reply)
            return

        elif operation == "send_client_send":
            send_client = parse.parse("{:d}, {:x}, {}", data)
            client_id = send_client.fixed[0]
            send_id = send_client.fixed[1]
            send = send_client.fixed[2]

            self.process_operation('send_client_send', send_id, time, send, client_id)
            return

        elif operation == "send_client_complete":
            send_client = parse.parse("{:d}, {:x}", data)
            client_id = send_client.fixed[0]
            send_id = send_client.fixed[1]

            self.process_operation('send_client_complete', send_id, time, '', client_id)
            return

        elif operation == "parse_client_msg":
            client_message = parse.parse("{:d}, {}", data)
            if client_message is None:
                print("Error: Failed to parse {}".format(data), file=sys.stderr)
            client_id = client_message.fixed[0]
            message = client_message.fixed[1]
            self.process_operation('parse_client_msg', client_id, time, message)
            return

        elif operation == "getblocktemplate":
            message = parse.parse("{:ti}, {:ti}, {}", data)
            start_time = message.fixed[0]
            finish_time = message.fixed[1]
            work_id = message.fixed[2]
            self.process_operation('getblocktemplate', work_id, start_time, '', finish_time)
            return

        elif operation == "blocksolve":
            """message = parse.parse("{}, {}, {}, {}", data)
            jobid = message.fixed[0]
            nonce = message.fixed[1]
            nonce2 = message.fixed[2]
            blockhash = message.fixed[3]
            return self.process_operation('blocksolve', jobid, time, (jobid, nonce, nonce2, blockhash))"""
            return

        elif operation == "submitblock":
            message = parse.parse("{:ti}, {:ti}, {}:{}, {}", data)
            start_time = message.fixed[0]
            finish_time = message.fixed[1]
            result = message.fixed[2]
            jobid = message.fixed[3]
            nonce = message.fixed[4]
            self.process_operation('submitblock', jobid, start_time, result, finish_time, nonce)
            return

        elif operation == "CallGBT":
            """message = parse.parse("{:ti}, {:ti}, {}", data)
            start_time = message.fixed[0]
            finish_time = message.fixed[1]
            result = message.fixed[2]
            #return self.process_operation('CallGGT', '', start_time, result, finish_time)"""
            return

        elif operation == "newblock":
            message = parse.parse("{}, {}", data)
            jobid = message.fixed[0]
            prevblockhash = message.fixed[1]
            self.process_operation('newblock', jobid, time, prevblockhash)
            return

        elif operation == "solution":
            message = parse.parse("{}, {}, {}, {}", data)
            jobid = message.fixed[0]
            nonce = message.fixed[1]
            btc_solution = message.fixed[2]
            rsk_solution = message.fixed[3]
            self.process_operation('solution', ":".join([jobid, nonce]), time, ":".join([btc_solution, rsk_solution]))
            return

        elif operation == "processSPVProof":
            message = parse.parse("{:ti}, {:ti}, {}", data)
            start_time = message.fixed[0]
            finish_time = message.fixed[1]
            blockhash = message.fixed[2]
            self.process_operation('processSPVProof', blockhash, start_time, '', finish_time)
            return

        print("Error: Failed to parse: |{}|".format(line), file=sys.stderr)
        raise ValueError("Unexpected line: {}".format(line))

    # Process individual operations and map them to a high livel operation
    def process_operation(self, operation, id, time, data, *args):
        if operation == 'json_rpc_call':
            # Before json-rpc call
            method = self.jsonrpc_method(data)
            self.server_calls[id] = (method, time, data)

        elif operation == 'json_rpc_reply':
            # After a successful json-rpc call
            if id in self.server_calls:
                #method, start_time, call = self.operations[id]

                #if method in ['submitblock']:
                #    pass
                #    # self.format(method, start_time, delta_ms(start_time, time))

                del self.server_calls[id]
            else:
                print("Error: json_rpc_reply {} without json_rpc_call {} at {}".format(id, data, time), file=sys.stderr)

        elif operation == 'send_client_send':
            # Before sending a message to a miner
            method = self.jsonrpc_method(data)
            self.client_calls[id] = (method, time, data)

        elif operation == 'send_client_complete':
            # After the message was sent to a miner
            if id in self.client_calls:
                client_id = args[0]
                method, start_time, call = self.client_calls[id]

                if method == 'mining.notify':
                    jobid = self.notify_jobid(self.client_calls[id][2])
                    self.log_action('mining.notify', start_time, delta_ms(start_time, time), ":".join([jobid, str(client_id)]))

                del self.client_calls[id]
            else:
                print("Error: send_client_complete {} without send_client_send {} at {}".format(id, data, time), file=sys.stderr)

        elif operation == 'parse_client_msg':
            # Message received from a miner
            method = self.jsonrpc_method(data)
            if method == 'mining.submit':
                jobid, nonce = self.submit_jobid(data)
                self.client_jobs.setdefault(jobid, {})[nonce] = (jobid, nonce, time)
                self.log_action(method, time, 0.0, ":".join([jobid, nonce]))

        elif operation == 'getblocktemplate':
            finish_time = args[0]
            self.log_action('getblocktemplate', time, delta_ms(time, finish_time), id)

        elif operation == 'blocksolve':
            """jobid, nonce, nonce2, blockhash = data
            if jobid in self.client_jobs and nonce in self.client_jobs[jobid]:
                sub_jobid, sub_nonce, sub_time = self.client_jobs[jobid][nonce]
                if jobid == sub_jobid:
                    self.submit_jobs[blockhash] = (sub_jobid, sub_time, sub_nonce, blockhash)
                    self.log_action('blocksolve', time, 0.0, ":".join([jobid, nonce, blockhash]))

                else:
                    print("Error blocksolve without valid job: {}, {}, {}".format(jobid, nonce, time))
            else:
                print("Error blocksolve without valid job: {}, {}, {}".format(jobid, nonce, time))"""

        elif operation == 'submitblock':
            finish_time = args[0]
            nonce = args[1]
            if id in self.client_jobs:
                if nonce in self.client_jobs[id]:
                    prev_jobid, prev_nonce, prev_time = self.client_jobs[id][nonce]
                    submit_id = ":".join([id, nonce])
                    self.submit_jobs[submit_id] = (prev_jobid, prev_time, prev_nonce, submit_id)
                    self.log_action('submitblock', time, delta_ms(time, finish_time), submit_id)
                    del self.submit_jobs[submit_id]

        elif operation == 'newblock':
            jobid = id
            prevblockhash = data
            self.log_action('newblock', time, 0.0, ":".join([jobid, prevblockhash]))

        elif operation == 'solution':
            self.log_action('solution', time, 0.0, ":".join([id, data]))

        elif operation == 'processSPVProof':
            finish_time = args[0]
            self.log_action('processSPVProof', time, delta_ms(time, finish_time), id)

    def log_action(self, method, start, duration, id=None):
        self.process_action(method, start, duration, id)

        if self.output:
            self.output.write("{}, {}, {}, {}\n".format(method, start, duration, '' if id is None else id))

    def process_action(self, method, start, duration, id=None):

        if method == 'getblocktemplate':
            if id in self.getblocktemplates:
                print("Error: getblocktemplate alread received {} at {}".format(id, start), file=sys.stderr)
                return

            if len(self.getblocktemplates) > 3:
                job_id = next(iter(self.getblocktemplates))
                prev_start, prev_duration, prev_id, prev_clients, last_client_start = self.getblocktemplates[job_id]
                last_client = "{:.3f}".format(delta_ms(prev_start, last_client_start) - prev_duration) if len(prev_clients) > 0 else '--'
                self.print_summary("getblocktemplate, {}, {}, {}, {}, {}".format(prev_start, prev_duration, prev_id, len(prev_clients), last_client))
                del self.getblocktemplates[job_id]

            self.getblocktemplates[id] = start, duration, id, {}, None

        elif method == 'mining.notify':
            [job_id, client_id] = id.split(":")
            if job_id in self.getblocktemplates:
                prev_start, prev_duration, prev_id, prev_clients, last_client_start = self.getblocktemplates[job_id]
                if client_id not in prev_clients:
                    prev_clients[client_id] = start
                    if last_client_start is None or delta_ms(last_client_start, start) <= args.max_notify_delta:
                        last_client_start = start
                self.getblocktemplates[job_id] = prev_start, prev_duration, prev_id, prev_clients, last_client_start
            else:
                #pendings = self.notify_pending.setdefault(job_id, [])
                #pendings += [(client_id, start)]
                print("Error: mining.notify without getblocktemplate: {}, {}".format(start, id), file=sys.stderr)
                raise ValueError("Error: mining.notify without getblocktemplate: {}, {}".format(start, id))

        elif method == 'submitblock':
            if id in self.submit_jobs:
                sub_jobid, sub_time, sub_nonce, blockhash = self.submit_jobs[id]
                self.print_summary("submitblock, {}, {}, {}, 1, {}".format(sub_time, delta_ms(sub_time, start), sub_jobid, duration))
            else:
                print("Error: submitblock without valid job: {}, {}".format(id, start), file=sys.stderr)

    def print_summary(self, message):
        if self.summary:
            print(message)

    def flush_info(self):
        for job_id, data in self.getblocktemplates.items():
            prev_start, prev_duration, prev_id, prev_clients, last_client_start = data
            last_client = "{:.3f}".format(delta_ms(prev_start, last_client_start) - prev_duration) if len(prev_clients) > 0 else '--'
            self.print_summary("getblocktemplate, {}, {}, {}, {}, {}".format(prev_start, prev_duration, prev_id, len(prev_clients), last_client))

        self.getblocktemplates = odict()

        self.submit_jobs = None

    def jsonrpc_method(self, data):
        """Recover the method from a json-rpc message"""
        try:
            try:
                call = json.loads(data)
            except json.decoder.JSONDecodeError as ex:
                if ex.msg == "Extra data":
                    call = json.loads(data[0:ex.pos])
                else:
                    raise ex
            if 'method' not in call:
                if 'result' in call:
                    return None
                print("Error: Not valid json-rpc call: {}".format(data), file=sys.stderr)
                return None
            return call['method']
        except json.decoder.JSONDecodeError as ex:
            pos = data.find('"method":')
            if pos >= 0:
                pos += 9
                if data[pos:pos+2] == ' "':
                    pos += 2
                elif data[pos:pos+1] == '"':
                    pos += 1
                end = data.find('"', pos)
                if end >= 0:
                    return data[pos:end]
            if data[0:22] == '{"id":null,"params":["':
                return "mining.notify"
            print("Error: Not valid json-rpc call: {}".format(data), file=sys.stderr)
            raise ex
        except:
            raise

        return None

    def notify_jobid(self, data):
        """Recover the jobid for a mining.notify message"""
        try:
            message = json.loads(data)
            return message['params'][0]
        except json.decoder.JSONDecodeError as ex:
            pos = data.find('"params":["')
            if pos >= 0:
                pos += 11
                end = data.find('"', pos)
                if end >= 0 and end - pos == 16:
                    return data[pos:end]
            print("Error: Failed to parse mining.notify: |{}|".format(data), file=sys.stderr)
            raise ex
        except:
            print("Error: Failed to parse mining.notify: |{}|".format(data), file=sys.stderr)
            raise

    def submit_jobid(self, data):
        """Recover the jobid from a mining.submit message"""
        try:
            message = json.loads(data)
            return message['params'][1], message['params'][4]
        except:
            print("Error: Failed to parse mining.submit: |{}|".format(data), file=sys.stderr)
            raise



def main():
    logFile = LogFile(args.logFile, args.output, args.summary, 'eloipool')
    logFile.parse()


if __name__ == "__main__":
    main()
