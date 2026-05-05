import subprocess
import signal
import paramiko
import glob
import shutil
from pathlib import Path
from subprocess import Popen, PIPE
import os
from paramiko import SSHClient, AutoAddPolicy
from concurrent.futures import ThreadPoolExecutor, as_completed
from scp import SCPClient
import threading
import time
import math
from threading import Lock
import argparse
import multiprocessing
import shlex


# Protocol metadata: quorum factor (e.g. 3f+1 vs 2f+1) and git branch for makeInstance checkout.
_PROTOCOL_CHECKOUT = {
    "HybridTEE": (3, "main"),
    "Chained-HybridTEE": (2, "main"),
    "Achilles": (2, "Achilles"),
    "Achilles-Recovery": (2, "Achilles"),
    "FlexiBFT": (3, "FlexiBFT"),
    "Damysus": (2, "Damysus"),
    "Oneshot": (2, "oneshot"),
    "Hotstuff": (3, "main"),
}


def protocol_factor(protocol: str) -> int:
    if protocol not in _PROTOCOL_CHECKOUT:
        raise ValueError(f"Unknown protocol: {protocol!r}")
    return _PROTOCOL_CHECKOUT[protocol][0]


def protocol_git_branch(protocol: str) -> str:
    if protocol not in _PROTOCOL_CHECKOUT:
        raise ValueError(f"Unknown protocol: {protocol!r}")
    return _PROTOCOL_CHECKOUT[protocol][1]


def num_replicas(factor: int, faults: int) -> int:
    return factor * faults + 1


# --- run.py CLI flags vs experiments.py (for comparable experiments) ---
# experiments.py uses --p1..--p8; run.py uses different numbering. Rough mapping:
#   run --p0 HybridTEE          ~ experiments --p4 (COMB / BASIC_HYBRID_TEE)
#   run --p01 Chained-Hybrid   ~ (no direct single flag; see experiments CH*)
#   run --p1 Achilles          ~ experiments Achilles branch
#   run --p2 FlexiBFT          ~ experiments multi-branch
#   run --p3 Damysus           ~ experiments --p6 chained COMB (CHAINED_CHEAP_AND_QUICK)
#   run --p4 Oneshot           ~ experiments --p8 (ONEP / BASIC_ONEP)
#   run --p5 Hotstuff          ~ experiments --p1 (BASE / BASIC_BASELINE)
# Local defaults aligned with experiments.py: numViews=10, numClTrans=1, config isTEE:1 for all nodes.


# --- Paths (single place to change repo layout) ---
PROJECT_ROOT = Path(__file__).resolve().parent
# Remote SSH/SCP tree (default: same as local checkout; override if needed)
REMOTE_PROJECT_ROOT = Path(os.environ.get("DAMYSUS_REMOTE_ROOT", str(PROJECT_ROOT)))

raw_ip_list = PROJECT_ROOT / "deployment" / "priv_ip.txt"
ip_list = PROJECT_ROOT / "ip_list"
clients = PROJECT_ROOT / "clients"
stats_dir = PROJECT_ROOT / "stats"
out_dir = PROJECT_ROOT / "out"
stats_txt = PROJECT_ROOT / "stats.txt"
close_py = PROJECT_ROOT / "close.py"
exen = PROJECT_ROOT / "exe"
client_stats_file = PROJECT_ROOT / "client_stats"


## Parameters
sgxmode     = "SIM"
#sgxmode      = "HW"
srcsgx       = "source /opt/intel/sgxsdk/environment" # this is where the sdk is supposed to be installed
statsdir     = "stats"        # stats directory (don't change, hard coded in C++)
params       = "App/params.h" # (don't change, hard coded in C++)
config       = "App/config.h" # (don't change, hard coded in C++)
addresses    = "config"       # (don't change, hard coded in C++)
useMultiCores = True
numMakeCores  = multiprocessing.cpu_count()  # number of cores to use to make
repeats      = 100 #10 #50 #5 #100 #2     # number of times to repeat each experiment
numViews     = 20     # number of views in each run
cutOffBound  = 60     # stop experiment after some time
#
numCTran     = 100   # number of transactions
numNonChCls  = 1     # number of clients for the non-chained versions
numChCls     = 1     # number of clients for the chained versions
numClTrans   = 1     # number of transactions sent by each clients
sleepTime    = 0     # start servers between 2 sends (in microseconds)
timeout      = 5     # timeout before changing changing leader (in seconds)
timeoutTime  = 240    #waiting time for the servers execution
kv_set_ratio = 30
kv_get_ratio = 60
kv_del_ratio = 10
kv_keyspace = 1000
kv_value_len = 16

#protocol settings

# fault       = 1
# factor      = 3
# numTrans    = 400
# payloadsize = 256
# counterDelay = 0
forcrmake = True
no_stash = True
# SSH defaults
SSH_USERNAME = 'root'
SSH_KEY_PATH = './TShard'

#deploy setting
numInstance   = 10 #number of instances run in a Machine
allLocalPorts = []    # list of all port numbers used in local experiments
ipsOfNodes    = {}    # dictionnary mapping node ids to IPs (local override)
startRport    = 8760
startCport    = 9760
startRedisPort = 6379
allLocalRedisPorts = []


# read IP list
def read_ip_list(filename):
    with open(filename, 'r') as file:
        ip_list = [line.strip() for line in file.readlines() if line.strip()]
    return ip_list

# read servers
def read_servers(total, filename):
    servers = []
    with open(filename, 'r') as file:
        for line in file:
            parts = line.strip().split()
            id = int(parts[0].split(':')[1])
            host = parts[1].split(':')[1]
            port1 = int(parts[2].split(':')[1])
            port2 = int(parts[3].split(':')[1])
            servers.append((id, host, port1, port2))
            if(len(servers) == total):
                break
    return servers


def count_tee_nodes_in_config(filename) -> int:
    """
    Count nodes marked as TEE in config file.
    Expected token format per line: ... isTEE:<0|1>
    """
    cfg_path = Path(filename)
    if not cfg_path.is_absolute():
        cfg_path = PROJECT_ROOT / filename
    tee_count = 0
    if not cfg_path.exists():
        return 0
    with open(cfg_path, "r") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            parts = line.split()
            for part in parts:
                if part.startswith("isTEE:"):
                    val = part.split(":", 1)[1]
                    tee_count += 1 if val == "1" else 0
                    break
    return tee_count

# send files to node
def scp_to_node(ip, files):
    ssh = SSHClient()
    ssh.set_missing_host_key_policy(AutoAddPolicy())
    ssh.connect(ip, username=SSH_USERNAME, key_filename=SSH_KEY_PATH)
    transport = ssh.get_transport()
    if transport is None:
        raise RuntimeError(f"Failed to get transport for {ip}")
    with SCPClient(transport) as scp:
        for file in files:
            remote_path = str(REMOTE_PROJECT_ROOT)
            scp.put(file, remote_path=remote_path)
    ssh.close()

# execute sgxserver
def ssh_exec_server_non_blocking(
    id,
    host,
    port1,
    port2,
    debug,
    factor,
    faults,
    totaltee,
    completion_set,
    lock,
    num_views,
    opdist,
):
    ssh = SSHClient()
    ssh.set_missing_host_key_policy(AutoAddPolicy())
    ssh.connect(host, username=SSH_USERNAME, key_filename=SSH_KEY_PATH)
    nodeType = "TEE" if id < totaltee else "nonTEE"
    if debug:
        cmd = f"export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/opt/intel/sgxsdk/sdk_libs && export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/usr/local/lib && cd {REMOTE_PROJECT_ROOT} && rm -rf stats/* && ./server {id} {nodeType} {totaltee} {faults} {factor} {num_views} {timeout} {opdist} > out{id}"
    else:
        cmd = f"export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/opt/intel/sgxsdk/sdk_libs && export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/usr/local/lib && cd {REMOTE_PROJECT_ROOT} && rm -rf stats/* && ./sgxserver {id} {nodeType} {totaltee} {faults} {factor} {num_views} {timeout} {opdist} > out{id}"
    stdin, stdout, stderr = ssh.exec_command(cmd)

    # Non-blocking monitoring of command execution status
    def monitor_ssh():
        stdout.channel.recv_exit_status() 
        output = stdout.read().decode()
        error = stderr.read().decode()
        # print(f"sgxserver on {host} with id {id} output:\n{output}")
        # print(f"sgxserver on {host} with id {id} error:\n{error}")
        with lock:
            completion_set.add((id, host))
        ssh.close()

    # start monitoring thread
    monitor_thread = threading.Thread(target=monitor_ssh)
    monitor_thread.start()

def ssh_exec_servers_non_blocking(
    servers,
    debug,
    factor,
    faults,
    totaltee,
    num_views,
    opdist,
    max_workers=6,
):
    completion_set = set()
    lock = Lock()
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                ssh_exec_server_non_blocking,
                server[0],
                server[1],
                server[2],
                server[3],
                debug,
                factor,
                faults,
                totaltee,
                completion_set,
                lock,
                num_views,
                opdist,
            ): server
            for server in servers
        }
        for future in as_completed(futures):
            server = futures[future]
            try:
                future.result()
                print(f"sgxserver on {server[1]} with id {server[0]} started successfully.")
            except Exception as e:
                print(f"sgxserver on {server[1]} with id {server[0]} generated an exception: {e}")
    
    # waiting sgxserver instances to finish

    total = factor * faults + 1

    l = 0


    while len(completion_set) < total:
        if(len(completion_set) > l):
            l = len(completion_set)
            print(f'finishied {l}')
        print(f'completion_set {len(completion_set)}')
        time.sleep(5)

# SSH execute sgxclient
def ssh_exec_client(id, host, port1, port2, extra_params, totaltee):
    ssh = SSHClient()
    ssh.set_missing_host_key_policy(AutoAddPolicy())
    ssh.connect(host, username=SSH_USERNAME, key_filename=SSH_KEY_PATH)
    cmd = f"sgxclient --id {id} --port1 {port1} --port2 {port2} {extra_params} {totaltee}"
    ssh.exec_command(cmd)
    ssh.close()

# acquire stats from node
def scp_from_node(ip):
    ssh = SSHClient()
    ssh.set_missing_host_key_policy(AutoAddPolicy())
    ssh.connect(ip, username=SSH_USERNAME, key_filename=SSH_KEY_PATH)
    transport = ssh.get_transport()
    if transport is None:
        raise RuntimeError(f"Failed to get transport for {ip}")

    with SCPClient(transport) as scp:
        scp.get('/remote/damysus/stats/*', local_path='damysus/stats/')  # 修改为实际的远程路径和本地路径
    ssh.close()

# send files to nodes
def scp_files_to_nodes(ip_list, files, max_workers=6):
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(scp_to_node, ip, files) for ip in ip_list]
        for future in futures:
            future.result()




#execute clients
def ssh_exec_client_on_id0(servers, extra_params, totaltee):
    id0_server = next((server for server in servers if server[0] == 0), None)
    if id0_server:
        time.sleep(5 + math.log(len(servers), 2))
        ssh_exec_client(id0_server[0], id0_server[1], id0_server[2], id0_server[3], extra_params, totaltee)

# acquire stats from nodes
def scp_files_from_nodes(ip_list):
    threads = []
    for ip in ip_list:
        thread = threading.Thread(target=scp_from_node, args=(ip,))
        threads.append(thread)
        thread.start()
    for thread in threads:
        thread.join()

def start_all_sgxservers(
    servers,
    debug,
    factor,
    faults,
    totaltee,
    num_views,
    opdist,
    max_workers=6,
):
    completion_set = set()
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(
                ssh_exec_server_non_blocking,
                id,
                host,
                port1,
                port2,
                debug,
                factor,
                faults,
                totaltee,
                completion_set,
                lock,
                num_views,
                opdist,
            )
            for id, host, port1, port2 in servers
        ]
        for future in as_completed(futures):
            future.result()
    return completion_set, lock


def ssh_start_redis_on_node(host: str, replica_id: int):
    """
    One Redis per replica on that replica's host, same layout as start_local_redis_instances:
    port = startRedisPort + replica_id, bind 127.0.0.1.
    """
    port = startRedisPort + replica_id
    rp = str(REMOTE_PROJECT_ROOT)
    workdir = f"{rp}/stats/redis/r{replica_id}"
    bash = (
        "if ! command -v redis-server >/dev/null 2>&1; then "
        f"echo warning: redis-server not found on {host}; exit 2; fi; "
        f"mkdir -p {shlex.quote(workdir)}; "
        f"fuser -k {port}/tcp 2>/dev/null || true; "
        "redis-server "
        f"--port {port} --save '' --appendonly no --daemonize yes "
        f"--dir {shlex.quote(workdir)} --bind 127.0.0.1 --protected-mode no "
        f"|| {{ echo warning: redis-server start failed on {host} r{replica_id}; exit 3; }}; "
        # daemonized redis can take a short while to bind; retry before failing.
        "ok=0; "
        "for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do "
        f"  if command -v redis-cli >/dev/null 2>&1; then "
        f"    redis-cli -h 127.0.0.1 -p {port} ping 2>/dev/null | grep -q PONG && ok=1 && break; "
        "  else "
        f"    (echo > /dev/tcp/127.0.0.1/{port}) >/dev/null 2>&1 && ok=1 && break; "
        "  fi; "
        "  sleep 0.5; "
        "done; "
        "[ \"$ok\" = \"1\" ] || exit 4"
    )
    ssh = SSHClient()
    ssh.set_missing_host_key_policy(AutoAddPolicy())
    ssh.connect(host, username=SSH_USERNAME, key_filename=SSH_KEY_PATH)
    stdin, stdout, stderr = ssh.exec_command("bash -lc " + shlex.quote(bash))
    exit_code = stdout.channel.recv_exit_status()
    err = stderr.read().decode().strip()
    ssh.close()
    if exit_code != 0:
        raise RuntimeError(
            f"remote redis setup/health-check failed on {host}:"
            f"{port} (replica {replica_id}), exit={exit_code}, err={err}"
        )


def start_remote_redis_for_servers(servers, max_workers=6):
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(ssh_start_redis_on_node, host, rid)
            for rid, host, _p1, _p2 in servers
        ]
        for future in as_completed(futures):
            future.result()

# execute sgxclient locally (same argv as start_local_client_process / experiment_local)
def local_exec_client(
    debug,
    factor,
    faults,
    totaltee,
    num_cl_trans,
    client_id,
    cl_sleep_us,
    rep,
    kv_set,
    kv_get,
    kv_del,
    kv_keys,
    kv_vlen,
):
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / f"client-cloud-rep{rep}-id{client_id}.log"
    log_fp = open(log_path, "w")
    client_bin = "./client" if debug else "./sgxclient"
    cmd = [
        client_bin,
        str(client_id),
        str(faults),
        str(factor),
        str(num_cl_trans),
        str(cl_sleep_us),
        str(rep),
        str(totaltee),
        str(kv_set),
        str(kv_get),
        str(kv_del),
        str(kv_keys),
        str(kv_vlen),
    ]
    proc = Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdout=log_fp,
        stderr=log_fp,
        preexec_fn=os.setsid,
    )
    # Keep file handle alive for process lifetime.
    proc._log_fp = log_fp  # type: ignore[attr-defined]
    return proc

def cp_client_stats():
    with open(client_stats_file, 'a') as f:
        f.write(f'numviews: {numViews}, numCTrans: {numCTran}, sleepttime: {sleepTime}\n')
    client_files = sorted(glob.glob(str(stats_dir / "client*")))
    if not client_files:
        # Keep this quiet when no client stats were produced in this run.
        return
    with open(client_stats_file, "a") as out:
        for fp in client_files:
            try:
                with open(fp, "r") as src:
                    out.write(src.read())
                out.write("\n")
            except OSError:
                continue
        out.write("\n")


# stop local sgxclient
def stop_local_sgxclient():
    cp_client_stats()

    cmd = "pkill -f sgxclient"
    process = Popen(cmd, shell=True, stdout=PIPE, stderr=PIPE)
    stdout, stderr = process.communicate()
    output = stdout.decode()
    error = stderr.decode()
    print(f"Stop sgxclient output:\n{output}")
    print(f"Stop sgxclient error:\n{error}")

def rm_local_stats():
    cmd = f"rm -rf {stats_dir}/*"
    process = Popen(cmd, shell=True, stdout=PIPE, stderr=PIPE)
    stdout, stderr = process.communicate()
    output = stdout.decode()
    error = stderr.decode()

def stop_remote_server():
    cmd = f"python3 {close_py}"
    subprocess.run(cmd, shell=True, check=True)


# Block and wait for all sgxserver instances to end
def wait_for_all_sgxservers_to_finish(completion_set, lock, total_servers):
    l = 0
    start_time = time.time()  # Start the timer
    while len(completion_set) < total_servers:
        if(len(completion_set) > l):
            l = len(completion_set)
            t = time.time()
            print(f'finishied {l}')
        # Check if the timeout has been reached
        if time.time() - start_time > timeoutTime:
            print(f"Timeout reached. Stopping remote server.")
            stop_remote_server()  # Stop the server if timeout
            break
    
    # If all servers finish, print a message
    if len(completion_set) == total_servers:
        print("All sgxserver instances have finished.")
            


def clear_local_stats():
    if stats_dir.exists() and stats_dir.is_dir():
        shutil.rmtree(stats_dir)
    stats_dir.mkdir(parents=True, exist_ok=True)

def cleanup_local_processes_and_ports(local_ports=None):
    """
    Best-effort cleanup for local experiments:
    - kill known server/client binaries
    - free the provided TCP ports with fuser
    """
    subprocess.run("killall -q sgxserver sgxclient server client redis-server", shell=True)
    if local_ports:
        ports = " ".join(map(lambda p: f"{p}/tcp", local_ports))
        subprocess.run(f"fuser -k {ports}", shell=True)
    # Give kernel/process manager a brief moment to release sockets fully.
    time.sleep(0.5)


def start_local_redis_instances(num_replicas: int):
    """
    Start one local Redis per replica (port = startRedisPort + replica_id).
    Uses daemon mode for simple lifecycle during local experiments.
    """
    global allLocalRedisPorts
    allLocalRedisPorts.clear()
    if shutil.which("redis-server") is None:
        print("warning: redis-server not found; KV app will fallback to in-memory backend")
        return

    # Always clean target Redis ports before a new local run.
    # This prevents stale daemonized redis-server processes from previous runs
    # (e.g. after Ctrl+Z / interrupted cleanup) from causing hangs/conflicts.
    target_ports = [startRedisPort + rid for rid in range(num_replicas)]
    if target_ports:
        ports_arg = " ".join(f"{p}/tcp" for p in target_ports)
        subprocess.run(f"fuser -k {ports_arg}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.2)

    redis_root = PROJECT_ROOT / "stats" / "redis"
    redis_root.mkdir(parents=True, exist_ok=True)
    for rid in range(num_replicas):
        port = startRedisPort + rid
        allLocalRedisPorts.append(port)
        workdir = redis_root / f"r{rid}"
        workdir.mkdir(parents=True, exist_ok=True)
        cmd = [
            "redis-server",
            "--port", str(port),
            "--save", "",
            "--appendonly", "no",
            "--daemonize", "yes",
            "--dir", str(workdir),
            "--bind", "127.0.0.1",
            "--protected-mode", "no",
        ]
        subprocess.run(cmd, check=False)


def start_local_server_process(
    server_bin: str,
    replica_id: int,
    node_type: str,
    effective_totaltee: int,
    faults: int,
    factor: int,
    num_views: int,
    timeout_val: int,
    local_opdist: int,
):
    """
    Start one local replica process without shell wrapping, so kill/terminate
    targets the real server process directly.
    """
    cmd = [
        server_bin,
        str(replica_id),
        node_type,
        str(effective_totaltee),
        str(faults),
        str(factor),
        str(num_views),
        str(timeout_val),
        str(local_opdist),
    ]
    return Popen(cmd, cwd=str(PROJECT_ROOT), preexec_fn=os.setsid)


def start_local_client_process(
    client_bin: str,
    client_id: int,
    faults: int,
    factor: int,
    num_cl_trans: int,
    cl_sleep_us: int,
    rep: int,
    effective_totaltee: int,
    kv_set: int,
    kv_get: int,
    kv_del: int,
    kv_keys: int,
    kv_vlen: int,
):
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / f"client-local-rep{rep}-id{client_id}.log"
    log_fp = open(log_path, "w")
    cmd = [
        client_bin,
        str(client_id),
        str(faults),
        str(factor),
        str(num_cl_trans),
        str(cl_sleep_us),
        str(rep),
        str(effective_totaltee),
        str(kv_set),
        str(kv_get),
        str(kv_del),
        str(kv_keys),
        str(kv_vlen),
    ]
    proc = Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdout=log_fp,
        stderr=log_fp,
        preexec_fn=os.setsid,
    )
    # Keep file handle alive for process lifetime.
    proc._log_fp = log_fp  # type: ignore[attr-defined]
    return proc


def kill_local_process_tree(proc: subprocess.Popen):
    """
    Kill the process group if possible (preferred), otherwise kill the process.
    """
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        proc.kill()

# SCP remote stats tree (under REMOTE_PROJECT_ROOT/stats/) into local_path (typically PROJECT_ROOT).
def scp_stats_from_node(ip, local_path, remote_path):
    ssh = SSHClient()
    ssh.set_missing_host_key_policy(AutoAddPolicy())
    ssh.connect(ip, username=SSH_USERNAME, key_filename=SSH_KEY_PATH)
    transport = ssh.get_transport()
    if transport is None:
        raise RuntimeError(f"Failed to get transport for {ip}")

    with SCPClient(transport) as scp:
        scp.get(remote_path, local_path=local_path, recursive=True)
    ssh.close()

# Multi-threaded SCP stats content to local
def scp_stats_from_nodes(ip_list, local_path, remote_path, max_workers=6):
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(scp_stats_from_node, ip, local_path, remote_path) for ip in ip_list]
        for future in as_completed(futures):
            future.result()


def clear_local_out_logs():
    out_dir.mkdir(parents=True, exist_ok=True)
    for fp in out_dir.glob("*"):
        if fp.is_file():
            fp.unlink()


def scp_out_logs_from_node(ip: str, local_out_path: str):
    """
    Fetch remote out* logs from REMOTE_PROJECT_ROOT into local out/ directory.
    Prefix each filename with source host to avoid collisions.
    """
    ssh = SSHClient()
    ssh.set_missing_host_key_policy(AutoAddPolicy())
    ssh.connect(ip, username=SSH_USERNAME, key_filename=SSH_KEY_PATH)
    sftp = ssh.open_sftp()
    try:
        remote_root = str(REMOTE_PROJECT_ROOT)
        for entry in sftp.listdir(remote_root):
            if not entry.startswith("out"):
                continue
            remote_fp = f"{remote_root}/{entry}"
            local_name = f"{ip.replace('.', '_')}-{entry}"
            local_fp = str(Path(local_out_path) / local_name)
            try:
                sftp.get(remote_fp, local_fp)
            except OSError:
                # Ignore transient or missing files and continue collecting others.
                continue
    finally:
        sftp.close()
        ssh.close()


def scp_out_logs_from_nodes(ip_list, local_out_path, max_workers=6):
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(scp_out_logs_from_node, ip, local_out_path) for ip in ip_list]
        for future in as_completed(futures):
            future.result()

def find_first_number(directory):
    rtt_files = glob.glob(os.path.join(directory, 'rtt-*'))

    for file_path in rtt_files:
        with open(file_path, 'r') as file:
            line = file.readline().strip()
            if line:
                try:
                    first_value = float(line.split()[0])
                    if not math.isnan(first_value):
                        return first_value
                except ValueError:
                    continue

    print("No valid data found.")
    return None

def calculate_mean_of_values(directory):
    vals_files = glob.glob(os.path.join(directory, 'vals*'))
    total_count = 0
    sum_first = 0.0
    sum_second = 0.0

    for file_path in vals_files:
        with open(file_path, 'r') as file:
            line = file.readline().strip()
            if line:
                values = list(map(float, line.split()))
                if len(values) >= 2:
                    sum_first += values[0]
                    sum_second += values[1]
                    print(f'{total_count}, {values[0]}, {values[1]}')
                    total_count += 1

    if total_count > 0:
        mean_first = sum_first / total_count
        mean_second = sum_second / total_count
        print(f"Mean of the first number across all vals files: {mean_first}")
        print(f"Mean of the second number across all vals files: {mean_second}")
        return (mean_first, mean_second)
    else:
        print("No vals files found or no valid data.")
        return (0, 0)


def compute_stats_like_experiments(stats_directory: str):
    """
    Same aggregation as experiments.computeStats: read each stats/vals* file
    (10 space-separated fields) and return view-averaged throughput/latency/etc.
    """
    sd = stats_directory.rstrip(os.sep) + os.sep
    throughput_view_val = 0.0
    throughput_view_num = 0
    latency_view_val = 0.0
    latency_view_num = 0
    handle_val = 0.0
    handle_num = 0
    crypto_sign_val = 0.0
    crypto_sign_num = 0
    crypto_verif_val = 0.0
    crypto_verif_num = 0
    crypto_num_sign_val = 0.0
    crypto_num_sign_num = 0
    crypto_num_verif_val = 0.0
    crypto_num_verif_num = 0

    for filename in glob.glob(sd + "vals*"):
        with open(filename, "r") as f:
            s = f.read()
        parts = s.split()
        if len(parts) != 10:
            print("wrong vals file:", filename)
            continue
        (
            thru,
            lat,
            hdl,
            _tos,
            _pbs,
            _pcs,
            sign_num,
            sign_time,
            verif_num,
            verif_time,
        ) = parts
        val_th = float(thru)
        throughput_view_num += 1
        throughput_view_val += val_th
        val_la = float(lat)
        latency_view_num += 1
        latency_view_val += val_la
        val_hd = float(hdl)
        handle_num += 1
        handle_val += val_hd
        val_st = float(sign_time)
        crypto_sign_num += 1
        crypto_sign_val += val_st
        val_vt = float(verif_time)
        crypto_verif_num += 1
        crypto_verif_val += val_vt
        val_sn = int(sign_num)
        crypto_num_sign_num += 1
        crypto_num_sign_val += float(val_sn)
        val_vn = int(verif_num)
        crypto_num_verif_num += 1
        crypto_num_verif_val += float(val_vn)

    throughput_view = throughput_view_val / throughput_view_num if throughput_view_num > 0 else 0.0
    latency_view = latency_view_val / latency_view_num if latency_view_num > 0 else 0.0
    handle = handle_val / handle_num if handle_num > 0 else 0.0
    crypto_sign = crypto_sign_val / crypto_sign_num if crypto_sign_num > 0 else 0.0
    crypto_verif = crypto_verif_val / crypto_verif_num if crypto_verif_num > 0 else 0.0
    crypto_num_sign = crypto_num_sign_val / crypto_num_sign_num if crypto_num_sign_num > 0 else 0.0
    crypto_num_verif = crypto_num_verif_val / crypto_num_verif_num if crypto_num_verif_num > 0 else 0.0

    print("throughput-view:", throughput_view, "out of", throughput_view_num)
    print("latency-view:", latency_view, "out of", latency_view_num)
    print("handle:", handle, "out of", handle_num)
    print("crypto-sign:", crypto_sign, "out of", crypto_sign_num)
    print("crypto-verif:", crypto_verif, "out of", crypto_verif_num)
    print("crypto-num-sign:", crypto_num_sign, "out of", crypto_num_sign_num)
    print("crypto-num-verif:", crypto_num_verif, "out of", crypto_num_verif_num)

    return (
        throughput_view,
        latency_view,
        handle,
        crypto_sign,
        crypto_verif,
        crypto_num_sign,
        crypto_num_verif,
    )


def parse_client_e2e_stats(stats_directory: str):
    """
    Parse client-e2e-* files written by App/Client.cpp and return averaged metrics.
    Format per file:
      key value
    where key includes reply_throughput_ktps, e2e_latency_avg_ms, e2e_latency_p50_ms,
    e2e_latency_p95_ms, e2e_latency_p99_ms, ...
    """
    e2e_files = glob.glob(os.path.join(stats_directory, "client-e2e-*"))
    if not e2e_files:
        return None

    metrics = {
        "reply_throughput_ktps": [],
        "e2e_latency_avg_ms": [],
        "e2e_latency_p50_ms": [],
        "e2e_latency_p95_ms": [],
        "e2e_latency_p99_ms": [],
    }
    parsed_files = 0
    for fp in e2e_files:
        file_vals = {}
        try:
            with open(fp, "r") as f:
                for raw in f:
                    line = raw.strip()
                    if not line:
                        continue
                    parts = line.split()
                    if len(parts) < 2:
                        continue
                    key, val = parts[0], parts[1]
                    if key in metrics:
                        try:
                            file_vals[key] = float(val)
                        except ValueError:
                            pass
        except OSError:
            continue

        if file_vals:
            parsed_files += 1
            for k in metrics:
                if k in file_vals:
                    metrics[k].append(file_vals[k])

    if parsed_files == 0:
        return None

    avgs = {}
    for k, vals in metrics.items():
        avgs[k] = (sum(vals) / len(vals)) if vals else 0.0
    avgs["num_client_e2e_files"] = parsed_files
    return avgs


def wait_local_client_procs(client_procs, deadline_sec: float):
    """
    Wait for all orchestrator-local client processes to finish and flush client-e2e-*.
    Any client still alive after deadline_sec is terminated.
    """
    if not client_procs:
        return
    deadline = time.time() + deadline_sec
    while time.time() < deadline:
        if all(p.poll() is not None for p in client_procs):
            return
        time.sleep(0.5)
    alive = [p for p in client_procs if p.poll() is None]
    if alive:
        print(
            f"[warn] {len(alive)} local client(s) still running after {deadline_sec}s; "
            "terminating so run.py can collect stats"
        )
        for p in alive:
            kill_local_process_tree(p)


def close_client_log_handles(client_procs):
    for p in client_procs:
        fp = getattr(p, "_log_fp", None)
        if fp is not None:
            try:
                fp.close()
            except OSError:
                pass


def plot_live_throughput_latency(stats_directory: str, out_png: str, out_csv: str):
    """
    Aggregate per-node stats/live-* samples into a single throughput/latency vs time curve.
    Expects live file line format:
      elapsedSec throughputView latencyView view
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # live samples are written into stats/ directory; file names are like live-<id>-<seconds>
    live_files = glob.glob(os.path.join(stats_directory, "live-*"))
    if not live_files:
        print("no live-* files found under", stats_directory)
        return

    samples_by_t = {}  # elapsed seconds (rounded) -> list[(thr,lat)]
    for fp in live_files:
        try:
            with open(fp, "r") as f:
                for raw in f:
                    line = raw.strip()
                    if not line:
                        continue
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    try:
                        t = float(parts[0])
                        # Round to 2 decimals for stable aggregation (0.1s cadence -> 1 decimal).
                        t_key = round(t, 2)
                        thr = float(parts[1])
                        lat = float(parts[2])
                    except ValueError:
                        continue
                    samples_by_t.setdefault(t_key, []).append((thr, lat))
        except FileNotFoundError:
            continue

    if not samples_by_t:
        print("live-* files exist but no parsable samples found")
        return

    secs = sorted(samples_by_t.keys())
    thr_avg = []
    lat_avg = []
    for s in secs:
        pairs = samples_by_t[s]
        thr_avg.append(sum(x[0] for x in pairs) / len(pairs))
        lat_avg.append(sum(x[1] for x in pairs) / len(pairs))

    # Write CSV for debugging/analysis
    with open(out_csv, "w") as f:
        f.write("sec throughput_tx_per_sec latency_p99_ms\n")
        for s, thr, lat in zip(secs, thr_avg, lat_avg):
            f.write(f"{s} {thr} {lat}\n")

    # Plot (two subplots)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7), sharex=True)
    ax1.plot(secs, thr_avg, marker="o", linewidth=1)
    ax1.set_ylabel("throughput (tx/s)")
    ax1.grid(True, linestyle="--", alpha=0.4)

    ax2.plot(secs, lat_avg, marker="o", color="orange", linewidth=1)
    ax2.set_xlabel("elapsed seconds")
    ax2.set_ylabel("latency p99 (ms)")
    ax2.grid(True, linestyle="--", alpha=0.4)

    fig.suptitle(f"Live throughput(tx/s) / latency p99(ms) curve ({os.path.basename(stats_directory.rstrip(os.sep))})")
    fig.tight_layout()  # type: ignore[attr-defined]
    fig.savefig(out_png)  # type: ignore[attr-defined]
    plt.close(fig)
    print("live curve written:", out_png, out_csv)


# make config
    # n: Total number of servers
def mkConfig(n, totaltee):

    def generate_servers(ip_list, n, numInstance):
        server_lines = []
        used_ips = []

        # Use exactly n replicas (e.g. n = 3f+1). Do NOT inflate n to a multiple of
        # numInstance — that was forcing faults=1,factor=3 from n=4 up to 10 servers and
        # wrong isTEE placement for cloud configs.

        port_start = 8760
        server_id = 0
        # For each port (instance index), enumerate all hosts in order before moving to next port
        for instance_index in range(numInstance):
            base_port = port_start + instance_index
            port1 = base_port
            port2 = base_port + 1000
            for ip in ip_list:
                if server_id >= n:
                    break
                if ip not in used_ips:
                    used_ips.append(ip)
                is_tee = 1 if server_id < totaltee else 0
                server_lines.append(f"id:{server_id} host:{ip} port:{port1} port:{port2} isTEE:{is_tee}")
                server_id += 1
            if server_id >= n:
                break
        
        return server_lines, used_ips

    def write_servers(filename, server_lines):
        with open(filename, 'w') as f:
            for line in server_lines:
                f.write(line + '\n')

    def write_ip_list(filename, used_ips):
        with open(filename, 'w') as f:
            for ip in used_ips:
                f.write(ip + '\n')

    def write_clients(filename, first_server_line):
        parts = first_server_line.split()
        client_line = (parts[0]+' '+ parts[1] + " port:8750 port:9750")
        with open(filename, 'w') as f:
            f.write(client_line + '\n')

    # Read IP list
    host_ips = read_ip_list(str(raw_ip_list))

    # Generate server configuration
    server_lines, used_ips = generate_servers(host_ips, n, numInstance)

    # Write to the configuration file
    write_servers(addresses, server_lines)
    write_ip_list(str(ip_list), used_ips)
    write_clients(str(clients), server_lines[0])
    
    return server_lines, used_ips
#end of mkConfig


## generates a local config file
def genLocalConf(n, filename, totaltee, *, all_is_tee_in_config=False):
    """
    If all_is_tee_in_config=True, every line has isTEE:1 (matches experiments.py local genLocalConf).
    Otherwise isTEE follows totaltee (id < totaltee -> 1).
    """
    open(filename, 'w').close()
    host = "127.0.0.1"

    global allLocalPorts
    allLocalPorts.clear()

    print("ips:", ipsOfNodes)

    f = open(filename, 'a')
    for i in range(n):
        host = ipsOfNodes.get(i, host)
        rport = startRport + i
        cport = startCport + i
        allLocalPorts.append(rport)
        allLocalPorts.append(cport)
        if all_is_tee_in_config:
            is_tee = 1
        else:
            is_tee = 1 if i < totaltee else 0
        f.write(
            "id:"
            + str(i)
            + " host:"
            + host
            + " port:"
            + str(rport)
            + " port:"
            + str(cport)
            + " isTEE:"
            + str(is_tee)
            + "\n"
        )
    f.close()
# End of genLocalConf



#make instance
def makeInstance(protocol, debug, batchsize, payload, faults, pct):

    pro_dir = str(exen / f"{protocol}_{faults}_{payload}_{batchsize}_{pct}")

    factor = protocol_factor(protocol)
    branch = protocol_git_branch(protocol)
    # change to the correct branch
    cmd_stash = 'git stash &&'
    if no_stash:
        cmd_stash = ' '
    cmd = f"{cmd_stash} git checkout {branch}"

    process = Popen(cmd, shell=True, stdout=PIPE, stderr=PIPE)
    stdout, stderr = process.communicate()
    output = stdout.decode()
    error = stderr.decode()
    print(f"Stop checkout output:\n{output}")
    print(f"Stop checkout error:\n{error}")

    # make params
    print(f"mkprotocol: {protocol}, factor:{factor}, batchsize: {batchsize}, payload: {payload}, teetotal: {faults}, pct: {pct}")
    mkParams(protocol,debug,factor,faults,batchsize,payload,pct)

    folder_path = pro_dir
    if not os.path.exists(folder_path):
    # If the folder does not exist, create
        os.makedirs(folder_path)
        print(f"Folder '{folder_path}' created.")
    else:
        print(f"Folder '{folder_path}' already exists.")

 
    # check if built binary and params.h exist (debug uses server/, release uses sgxserver)
    if debug:
        server_path = os.path.join(folder_path, "server")
        params_h_path = os.path.join(folder_path, "params.h")
        server_exists = os.path.isfile(server_path)
        params_h_exists = os.path.isfile(params_h_path)
    else:
        sgxserver_path = os.path.join(folder_path, "sgxserver")
        params_h_path = os.path.join(folder_path, "params.h")
        server_exists = os.path.isfile(sgxserver_path)
        params_h_exists = os.path.isfile(params_h_path)
    
    # need to make or not
    need_make = True
    if server_exists and params_h_exists and not forcrmake:
        need_make = False
        print("Files exist and force make is disabled, skipping make")

    # make (debug: server+client; else: sgxserver)
    if need_make:
        print("Starting make process...")
        subprocess.call(["make","clean"])
        if debug:
            subprocess.call(["make","-j8","server","client"])
            subprocess.call(["cp", "server", f"{pro_dir}/"])
            subprocess.call(["cp", "App/params.h", f"{pro_dir}/"]) 
        else:
            subprocess.run(["bash -c \"" + srcsgx + "\""], shell=True, check=True)
            subprocess.call(["make","-j",str(numMakeCores),"SGX_MODE="+sgxmode])
            subprocess.call(["cp", "sgxserver", f"{pro_dir}/"]) 
            subprocess.call(["cp", "App/params.h", f"{pro_dir}/"]) 
        print("make finished")
    else:
        print("Skipping make process")
#end of makeInstance

# make params
def mkParams(protocol,debug,constFactor,numFaults,numTrans,payloadSize,pct):
    f = open(params, 'w')
    f.write("#ifndef PARAMS_H\n")
    f.write("#define PARAMS_H\n")
    f.write("\n")
    # f.write("#define " + protocol.value + "\n")
    if protocol == "HybridTEE":
        if debug:
            f.write("#define BASIC_HYBRID_TEE_DEBUG\n")
        else:
            f.write("#define BASIC_HYBRID_TEE\n")
    elif protocol == "Chained-HybridTEE":
        f.write("#define CHAINED_CHEAP_AND_QUICK\n")
    elif protocol == "Achilles":
        f.write("#define ACHILLES\n")
    elif protocol == "FlexiBFT":
        f.write("#define CHAINED_CHEAP_AND_QUICK\n")
    elif protocol == "Damysus":
        f.write("#define CHAINED_CHEAP_AND_QUICK\n")
    elif protocol == "Oneshot":
        f.write("#define BASIC_ONEP\n")
    elif protocol == "Hotstuff":
        f.write("#define BASIC_BASELINE\n")
    f.write("#define MAX_NUM_NODES " + str((constFactor*numFaults)+1) + "\n")

    # if protocol == "Chained-HybridTEE":
    #     f.write("#define MAX_NUM_SIGNATURES " + str(numFaults+1) + "\n")
    # else:
    #     f.write("#define MAX_NUM_SIGNATURES " + str((constFactor*numFaults)+1-numFaults) + "\n")
    f.write("#define MAX_NUM_SIGNATURES " + str((constFactor*numFaults)+1-numFaults) + "\n")
    f.write("#define MAX_NUM_TEE_SIGNATURES " + str(numFaults+1) + "\n")
    f.write("#define MAX_NUM_TRANSACTIONS " + str(numTrans) + "\n")
    f.write("#define PAYLOAD_SIZE " +str(payloadSize) + "\n")
    f.write("#define PERSISTENT_COUNTER_TIME " +str(pct) + "\n")
    f.write("\n")
    f.write("#endif\n")
    f.close()
# End of mkParams






def experiment_local(
    protocol,
    debug,
    batchsize,
    payload,
    faults,
    totaltee,
    pct,
    *,
    num_views=10,
    num_cl_trans=1,
    num_clients=1,
    cl_sleep_us=50,
    repeats=1,
    config_by_totaltee=True,
    local_opdist=0,
    print_vals_means=False,
    cutoff_sec=None,
):
    """
    Local run aligned with experiments.py: execute() + computeStats() + computeAvgStats() loop.

    - num_views: per-server view budget (experiments default 10).
    - num_cl_trans: transactions sent by each client (argv[4], default 1).
    - num_clients: number of client processes to start (default 1).
    - cl_sleep_us: client send interval in microseconds (argv[5], default 50).
    - batchsize: still only used for compile-time MAX_NUM_TRANSACTIONS via makeInstance/mkParams.
    - config_by_totaltee: if True, config uses --totaltee (first totaltee nodes are isTEE:1).
    """
    factor = protocol_factor(protocol)
    total = num_replicas(factor, faults)
    pro_dir = f"{protocol}_{faults}_{totaltee}_{payload}_{batchsize}_{pct}"

    server_bin = "./server" if debug else "./sgxserver"
    client_bin = "./client" if debug else "./sgxclient"
    stats_directory = str(stats_dir) + os.sep

    throughput_views = []
    latency_views = []
    handles = []
    crypto_signs = []
    crypto_verifs = []
    crypto_num_signs = []
    crypto_num_verifs = []
    e2e_reply_throughputs = []
    e2e_lat_avg = []
    e2e_lat_p50 = []
    e2e_lat_p95 = []
    e2e_lat_p99 = []
    good_values = 0

    for rep in range(repeats):
        # Fault-mode runs may need more time after killing one replica.
        # Keep user-provided --cutoff-sec as highest priority; otherwise use
        # a longer default than the non-fault local path.
        run_cutoff = int(cutoff_sec) if cutoff_sec is not None else timeoutTime
        print(
            ">>>>>>>>>>>>>>>>>>>>",
            f"protocol={protocol}",
            f"payload={payload}",
            f"factor={factor}",
            f"faults={faults}",
            f"repeat={rep}",
            f"num_views={num_views}",
            f"num_cl_trans={num_cl_trans}",
            f"cutoff_sec={run_cutoff}",
        )
        genLocalConf(
            total,
            addresses,
            totaltee,
            all_is_tee_in_config=not config_by_totaltee,
        )
        effective_totaltee = count_tee_nodes_in_config(addresses)
        effective_pro_dir = f"{protocol}_{faults}_{effective_totaltee}_{payload}_{batchsize}_{pct}"
        if rep == 0:
            pro_dir = effective_pro_dir
        # Cleanup BEFORE startup to avoid "Address already in use" from stale processes.
        cleanup_local_processes_and_ports(allLocalPorts + allLocalRedisPorts)
        clear_local_stats()
        start_local_redis_instances(total)

        rep_procs = []
        client_procs = []
        try:
            for i in range(total):
                if i % 10 == 5:
                    time.sleep(2)
                # Keep runtime node type consistent with config's isTEE count.
                node_type = "TEE" if i < effective_totaltee else "nonTEE"
                p = start_local_server_process(
                    server_bin,
                    i,
                    node_type,
                    effective_totaltee,
                    faults,
                    factor,
                    num_views,
                    timeout,
                    local_opdist,
                )
                rep_procs.append(("R", i, p))

            print("started", len(rep_procs), "replicas")

            wait = 5 + int(math.ceil(math.log(faults, 2)))
            time.sleep(wait)

            for client_id in range(num_clients):
                client_proc = start_local_client_process(
                    client_bin,
                    client_id,
                    faults,
                    factor,
                    num_cl_trans,
                    cl_sleep_us,
                    rep,
                    effective_totaltee,
                    kv_set_ratio,
                    kv_get_ratio,
                    kv_del_ratio,
                    kv_keyspace,
                    kv_value_len,
                )
                client_procs.append(client_proc)
            print("started", len(client_procs), "clients")

            total_time = 0
            remaining = rep_procs.copy()
            replicas_done_since = None
            client_grace_sec = 5
            client_done_since = None
            server_after_client_grace_sec = 5
            while total_time < run_cutoff:
                updated_remaining = []
                for (tag, rid, proc) in remaining:
                    done_file_ready = len(glob.glob(statsdir + "/done-" + str(rid) + "*")) > 0
                    proc_exited = (proc.poll() is not None)
                    if not done_file_ready and not proc_exited:
                        updated_remaining.append((tag, rid, proc))
                remaining = updated_remaining
                client_exited = bool(client_procs) and all(p.poll() is not None for p in client_procs)
                client_e2e_ready = len(glob.glob(statsdir + "/client-e2e-*")) >= num_clients
                if client_exited or client_e2e_ready:
                    if client_done_since is None:
                        client_done_since = time.time()
                # If client is done but some server does not exit / write done-*,
                # do not block too long; proceed to cleanup and stats aggregation.
                if client_done_since is not None and len(remaining) > 0:
                    if (time.time() - client_done_since) >= server_after_client_grace_sec:
                        print(
                            f"[warn] client finished but {len(remaining)} server(s) still pending after "
                            f"{server_after_client_grace_sec}s; continue with cleanup"
                        )
                        break
                if len(remaining) == 0:
                    if replicas_done_since is None:
                        replicas_done_since = time.time()
                    if client_exited or client_e2e_ready:
                        break
                    if (time.time() - replicas_done_since) >= client_grace_sec:
                        print(
                            f"[warn] replicas done but client still running after {client_grace_sec}s; "
                            "continue with cleanup"
                        )
                        break
                print(
                    "remaining processes:",
                    remaining,
                    "client_exited=",
                    client_exited,
                    "client_e2e_ready=",
                    client_e2e_ready,
                )
                time.sleep(1)
                total_time += 1

            if total_time < run_cutoff:
                print("all", len(rep_procs) + len(client_procs), "processes are done")
            else:
                print(f"------ reached cutoff bound ({run_cutoff}s) ------")

        finally:
            for (_, i, p) in rep_procs:
                if p.poll() is None:
                    print("still running:", ("R", i, p.poll()))
                    kill_local_process_tree(p)
            for idx, p in enumerate(client_procs):
                if p.poll() is None:
                    print("still running:", ("C", idx, p.poll()))
                    kill_local_process_tree(p)
            close_client_log_handles(client_procs)
            # Always cleanup ports even on exceptions / KeyboardInterrupt.
            cleanup_local_processes_and_ports(allLocalPorts + allLocalRedisPorts)

        (
            throughput_view,
            latency_view,
            handle,
            crypto_sign,
            crypto_verif,
            crypto_num_sign,
            crypto_num_verif,
        ) = compute_stats_like_experiments(stats_directory)

        # Live curve: aggregate stats/live-* written during this repeat.
        live_out_png = str(PROJECT_ROOT / "stats" / f"live-curve-{pro_dir}-rep{rep}.png")
        live_out_csv = str(PROJECT_ROOT / "stats" / f"live-curve-{pro_dir}-rep{rep}.csv")
        plot_live_throughput_latency(stats_directory, live_out_png, live_out_csv)

        # Fault experiments may end before recordStats() writes vals/done; in that case,
        # fall back to the last live curve point so you still get throughput/latency output.
        vals_present = len(glob.glob(stats_directory + "/vals-*")) > 0
        if not vals_present and os.path.exists(live_out_csv):
            try:
                with open(live_out_csv, "r") as f:
                    last = None
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("sec"):
                            continue
                        last = line
                if last:
                    _sec, thr_s, lat_s = last.split()
                    throughput_view = float(thr_s)
                    latency_view = float(lat_s)
            except Exception:
                pass

        # For fault-injection runs, we mainly care about throughput/latency.
        # Other counters (handle/crypto-*) may legitimately drop to 0 after a crash.
        good_cond = throughput_view > 0 and latency_view > 0

        if good_cond:
            throughput_views.append(throughput_view)
            latency_views.append(latency_view)
            handles.append(handle)
            crypto_signs.append(crypto_sign)
            crypto_verifs.append(crypto_verif)
            crypto_num_signs.append(crypto_num_sign)
            crypto_num_verifs.append(crypto_num_verif)
            good_values += 1

        if print_vals_means:
            calculate_mean_of_values(stats_directory)
        e2e_stats = parse_client_e2e_stats(stats_directory)
        if e2e_stats is not None:
            e2e_reply_throughputs.append(e2e_stats["reply_throughput_ktps"])
            e2e_lat_avg.append(e2e_stats["e2e_latency_avg_ms"])
            e2e_lat_p50.append(e2e_stats["e2e_latency_p50_ms"])
            e2e_lat_p95.append(e2e_stats["e2e_latency_p95_ms"])
            e2e_lat_p99.append(e2e_stats["e2e_latency_p99_ms"])
            print(
                "client e2e (rep",
                rep,
                "):",
                "reply_ktps=",
                e2e_stats["reply_throughput_ktps"],
                "avg_ms=",
                e2e_stats["e2e_latency_avg_ms"],
                "p95_ms=",
                e2e_stats["e2e_latency_p95_ms"],
                "p99_ms=",
                e2e_stats["e2e_latency_p99_ms"],
                "files=",
                int(e2e_stats["num_client_e2e_files"]),
            )
        cp_client_stats()

    avg_throughput = sum(throughput_views) / good_values if good_values > 0 else 0.0
    avg_latency = sum(latency_views) / good_values if good_values > 0 else 0.0
    avg_handle = sum(handles) / good_values if good_values > 0 else 0.0
    avg_crypto_sign = sum(crypto_signs) / good_values if good_values > 0 else 0.0
    avg_crypto_verif = sum(crypto_verifs) / good_values if good_values > 0 else 0.0
    avg_crypto_num_sign = sum(crypto_num_signs) / good_values if good_values > 0 else 0.0
    avg_crypto_num_verif = sum(crypto_num_verifs) / good_values if good_values > 0 else 0.0
    avg_e2e_reply_tps = (
        sum(e2e_reply_throughputs) / len(e2e_reply_throughputs)
        if e2e_reply_throughputs
        else 0.0
    )
    avg_e2e_lat_avg_ms = (sum(e2e_lat_avg) / len(e2e_lat_avg)) if e2e_lat_avg else 0.0
    avg_e2e_lat_p50_ms = (sum(e2e_lat_p50) / len(e2e_lat_p50)) if e2e_lat_p50 else 0.0
    avg_e2e_lat_p95_ms = (sum(e2e_lat_p95) / len(e2e_lat_p95)) if e2e_lat_p95 else 0.0
    avg_e2e_lat_p99_ms = (sum(e2e_lat_p99) / len(e2e_lat_p99)) if e2e_lat_p99 else 0.0

    print("avg throughput (view):", avg_throughput)
    print("avg latency (view):", avg_latency)
    print("avg handle:", avg_handle)
    print("avg crypto (sign):", avg_crypto_sign)
    print("avg crypto (verif):", avg_crypto_verif)
    print("avg crypto (sign-num):", avg_crypto_num_sign)
    print("avg crypto (verif-num):", avg_crypto_num_verif)
    print("avg client reply throughput (e2e):", avg_e2e_reply_tps)
    print("avg client e2e latency avg (ms):", avg_e2e_lat_avg_ms)
    print("avg client e2e latency p50 (ms):", avg_e2e_lat_p50_ms)
    print("avg client e2e latency p95 (ms):", avg_e2e_lat_p95_ms)
    print("avg client e2e latency p99 (ms):", avg_e2e_lat_p99_ms)
    print("num e2e repeats=", len(e2e_reply_throughputs))
    print("num complete runs=", repeats)
    print("num good stat runs=", good_values)

    with open(stats_txt, 'a') as f:
        f.write(
            f"{pro_dir}, thr_view={avg_throughput}, lat_view={avg_latency}, "
            f"e2e_reply_tps={avg_e2e_reply_tps}, e2e_lat_avg={avg_e2e_lat_avg_ms}, "
            f"e2e_lat_p50={avg_e2e_lat_p50_ms}, e2e_lat_p95={avg_e2e_lat_p95_ms}, "
            f"e2e_lat_p99={avg_e2e_lat_p99_ms}, good={good_values}/{repeats},\n"
        )
    print(
        pro_dir,
        "thr_view=",
        avg_throughput,
        "lat_view=",
        avg_latency,
        "e2e_reply_tps=",
        avg_e2e_reply_tps,
        "e2e_p95=",
        avg_e2e_lat_p95_ms,
        "e2e_p99=",
        avg_e2e_lat_p99_ms,
    )


def experiment_fault_local(
    protocol,
    debug,
    batchsize,
    payload,
    faults,
    totaltee,
    pct,
    *,
    dead_node_id: int = 1,
    kill_after_sec: float = 2.0,
    num_views=10,
    num_cl_trans=1,
    num_clients=1,
    cl_sleep_us=50,
    repeats=1,
    config_by_totaltee=True,
    local_opdist=0,
    print_vals_means=False,
    cutoff_sec=None,
):
    """
    Local experiment with one replica crash during execution.

    - dead_node_id: replica index to kill (0..total-1)
    - kill_after_sec: seconds after starting client
    """
    factor = protocol_factor(protocol)
    total = num_replicas(factor, faults)
    pro_dir = f"{protocol}_{faults}_{totaltee}_{payload}_{batchsize}_{pct}"

    server_bin = "./server" if debug else "./sgxserver"
    client_bin = "./client" if debug else "./sgxclient"
    stats_directory = str(stats_dir) + os.sep

    throughput_views = []
    latency_views = []
    handles = []
    crypto_signs = []
    crypto_verifs = []
    crypto_num_signs = []
    crypto_num_verifs = []
    e2e_reply_throughputs = []
    e2e_lat_avg = []
    e2e_lat_p50 = []
    e2e_lat_p95 = []
    e2e_lat_p99 = []
    good_values = 0

    for rep in range(repeats):
        run_cutoff = int(cutoff_sec) if cutoff_sec is not None else cutOffBound
        print(
            ">>>>>>>>>>>>>>>>>>>>",
            f"protocol={protocol}",
            f"payload={payload}",
            f"factor={factor}",
            f"faults={faults}",
            f"repeat={rep}",
            f"num_views={num_views}",
            f"num_cl_trans={num_cl_trans}",
            f"dead_node_id={dead_node_id}",
            f"kill_after_sec={kill_after_sec}",
            f"cutoff_sec={run_cutoff}",
        )

        genLocalConf(
            total,
            addresses,
            totaltee,
            all_is_tee_in_config=not config_by_totaltee,
        )
        effective_totaltee = count_tee_nodes_in_config(addresses)
        effective_pro_dir = f"{protocol}_{faults}_{effective_totaltee}_{payload}_{batchsize}_{pct}"
        if rep == 0:
            pro_dir = effective_pro_dir

        cleanup_local_processes_and_ports(allLocalPorts + allLocalRedisPorts)
        clear_local_stats()
        start_local_redis_instances(total)

        rep_procs = []
        client_procs = []
        killed_dead_node = False
        client_start_ts = None
        try:
            for i in range(total):
                if i % 10 == 5:
                    time.sleep(2)
                node_type = "TEE" if i < effective_totaltee else "nonTEE"
                p = start_local_server_process(
                    server_bin,
                    i,
                    node_type,
                    effective_totaltee,
                    faults,
                    factor,
                    num_views,
                    timeout,
                    local_opdist,
                )
                rep_procs.append(("R", i, p))

            print("started", len(rep_procs), "replicas")

            wait = 5 + int(math.ceil(math.log(faults, 2)))
            time.sleep(wait)

            for client_id in range(num_clients):
                client_proc = start_local_client_process(
                    client_bin,
                    client_id,
                    faults,
                    factor,
                    num_cl_trans,
                    cl_sleep_us,
                    rep,
                    effective_totaltee,
                    kv_set_ratio,
                    kv_get_ratio,
                    kv_del_ratio,
                    kv_keyspace,
                    kv_value_len,
                )
                client_procs.append(client_proc)
            print("started", len(client_procs), "clients")
            client_start_ts = time.time()

            total_time = 0
            remaining = rep_procs.copy()
            replicas_done_since = None
            client_grace_sec = 5
            client_done_since = None
            server_after_client_grace_sec = 5
            while total_time < run_cutoff:
                # Kill the target replica once during the run.
                if (not killed_dead_node) and (client_start_ts is not None):
                    if (time.time() - client_start_ts) >= kill_after_sec:
                        for (_, i, p) in rep_procs:
                            if i == dead_node_id and p.poll() is None:
                                print(f"[fault] killing replica {i} at t≈{time.time() - client_start_ts:.2f}s")
                                kill_local_process_tree(p)
                                time.sleep(0.2)
                                print(f"[fault] replica {i} poll={p.poll()} (None means still alive)")
                        # Remove it from waiting list so we don't block on done-*.
                        remaining = list(filter(lambda x: x[1] != dead_node_id, remaining))
                        killed_dead_node = True

                updated_remaining = []
                for (tag, rid, proc) in remaining:
                    done_file_ready = len(glob.glob(statsdir + "/done-" + str(rid) + "*")) > 0
                    proc_exited = (proc.poll() is not None)
                    if not done_file_ready and not proc_exited:
                        updated_remaining.append((tag, rid, proc))
                remaining = updated_remaining
                client_exited = bool(client_procs) and all(p.poll() is not None for p in client_procs)
                client_e2e_ready = len(glob.glob(statsdir + "/client-e2e-*")) >= num_clients
                if client_exited or client_e2e_ready:
                    if client_done_since is None:
                        client_done_since = time.time()
                if client_done_since is not None and len(remaining) > 0:
                    if (time.time() - client_done_since) >= server_after_client_grace_sec:
                        print(
                            f"[warn] client finished but {len(remaining)} server(s) still pending after "
                            f"{server_after_client_grace_sec}s; continue with cleanup (fault mode)"
                        )
                        break
                if len(remaining) == 0:
                    if replicas_done_since is None:
                        replicas_done_since = time.time()
                    if client_exited or client_e2e_ready:
                        break
                    if (time.time() - replicas_done_since) >= client_grace_sec:
                        print(
                            f"[warn] replicas done but client still running after {client_grace_sec}s; "
                            "continue with cleanup (fault mode)"
                        )
                        break
                print(
                    "remaining processes:",
                    remaining,
                    "client_exited=",
                    client_exited,
                    "client_e2e_ready=",
                    client_e2e_ready,
                )
                time.sleep(1)
                total_time += 1

            if total_time < run_cutoff:
                print("all", len(rep_procs) + len(client_procs), "processes are done (fault mode)")
            else:
                print(
                    "------ reached cutoff bound "
                    f"(fault mode, {run_cutoff}s). "
                    "Processes are force-stopped by run.py. "
                    "Use --cutoff-sec to increase this window. ------"
                )

        finally:
            for (_, i, p) in rep_procs:
                if p.poll() is None:
                    print("still running:", ("R", i, p.poll()))
                    kill_local_process_tree(p)
            for idx, p in enumerate(client_procs):
                if p.poll() is None:
                    print("still running:", ("C", idx, p.poll()))
                    kill_local_process_tree(p)
            close_client_log_handles(client_procs)
            cleanup_local_processes_and_ports(allLocalPorts + allLocalRedisPorts)

        (
            throughput_view,
            latency_view,
            handle,
            crypto_sign,
            crypto_verif,
            crypto_num_sign,
            crypto_num_verif,
        ) = compute_stats_like_experiments(stats_directory)

        live_out_png = str(PROJECT_ROOT / "stats" / f"live-curve-{pro_dir}-rep{rep}-fault.png")
        live_out_csv = str(PROJECT_ROOT / "stats" / f"live-curve-{pro_dir}-rep{rep}-fault.csv")
        plot_live_throughput_latency(stats_directory, live_out_png, live_out_csv)

        # If fault kills consensus before recordStats() writes vals/done,
        # fall back to the last live curve point so we still output throughput/latency.
        vals_present = len(glob.glob(stats_directory + "/vals-*")) > 0
        if not vals_present and os.path.exists(live_out_csv):
            try:
                with open(live_out_csv, "r") as f:
                    last = None
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("sec"):
                            continue
                        last = line
                if last:
                    _sec, thr_s, lat_s = last.split()
                    throughput_view = float(thr_s)
                    latency_view = float(lat_s)
            except Exception:
                pass

        # For fault-injection runs, we mainly care about throughput/latency.
        # Other counters (handle/crypto-*) may legitimately drop to 0 after a crash.
        good_cond = throughput_view > 0 and latency_view > 0

        if good_cond:
            throughput_views.append(throughput_view)
            latency_views.append(latency_view)
            handles.append(handle)
            crypto_signs.append(crypto_sign)
            crypto_verifs.append(crypto_verif)
            crypto_num_signs.append(crypto_num_sign)
            crypto_num_verifs.append(crypto_num_verif)
            good_values += 1

        if print_vals_means:
            calculate_mean_of_values(stats_directory)
        e2e_stats = parse_client_e2e_stats(stats_directory)
        if e2e_stats is not None:
            e2e_reply_throughputs.append(e2e_stats["reply_throughput_ktps"])
            e2e_lat_avg.append(e2e_stats["e2e_latency_avg_ms"])
            e2e_lat_p50.append(e2e_stats["e2e_latency_p50_ms"])
            e2e_lat_p95.append(e2e_stats["e2e_latency_p95_ms"])
            e2e_lat_p99.append(e2e_stats["e2e_latency_p99_ms"])
            print(
                "client e2e (rep",
                rep,
                "fault):",
                "reply_ktps=",
                e2e_stats["reply_throughput_ktps"],
                "avg_ms=",
                e2e_stats["e2e_latency_avg_ms"],
                "p95_ms=",
                e2e_stats["e2e_latency_p95_ms"],
                "p99_ms=",
                e2e_stats["e2e_latency_p99_ms"],
                "files=",
                int(e2e_stats["num_client_e2e_files"]),
            )
        cp_client_stats()

    avg_throughput = sum(throughput_views) / good_values if good_values > 0 else 0.0
    avg_latency = sum(latency_views) / good_values if good_values > 0 else 0.0
    avg_handle = sum(handles) / good_values if good_values > 0 else 0.0
    avg_crypto_sign = sum(crypto_signs) / good_values if good_values > 0 else 0.0
    avg_crypto_verif = sum(crypto_verifs) / good_values if good_values > 0 else 0.0
    avg_crypto_num_sign = sum(crypto_num_signs) / good_values if good_values > 0 else 0.0
    avg_crypto_num_verif = sum(crypto_num_verifs) / good_values if good_values > 0 else 0.0
    avg_e2e_reply_tps = (
        sum(e2e_reply_throughputs) / len(e2e_reply_throughputs)
        if e2e_reply_throughputs
        else 0.0
    )
    avg_e2e_lat_avg_ms = (sum(e2e_lat_avg) / len(e2e_lat_avg)) if e2e_lat_avg else 0.0
    avg_e2e_lat_p50_ms = (sum(e2e_lat_p50) / len(e2e_lat_p50)) if e2e_lat_p50 else 0.0
    avg_e2e_lat_p95_ms = (sum(e2e_lat_p95) / len(e2e_lat_p95)) if e2e_lat_p95 else 0.0
    avg_e2e_lat_p99_ms = (sum(e2e_lat_p99) / len(e2e_lat_p99)) if e2e_lat_p99 else 0.0

    print("avg throughput (view):", avg_throughput)
    print("avg latency (view):", avg_latency)
    print("avg handle:", avg_handle)
    print("avg crypto (sign):", avg_crypto_sign)
    print("avg crypto (verif):", avg_crypto_verif)
    print("avg crypto (sign-num):", avg_crypto_num_sign)
    print("avg crypto (verif-num):", avg_crypto_num_verif)
    print("avg client reply throughput (e2e):", avg_e2e_reply_tps)
    print("avg client e2e latency avg (ms):", avg_e2e_lat_avg_ms)
    print("avg client e2e latency p50 (ms):", avg_e2e_lat_p50_ms)
    print("avg client e2e latency p95 (ms):", avg_e2e_lat_p95_ms)
    print("avg client e2e latency p99 (ms):", avg_e2e_lat_p99_ms)
    print("num e2e repeats=", len(e2e_reply_throughputs))
    print("num complete runs=", repeats)
    print("num good stat runs=", good_values)

    with open(stats_txt, "a") as f:
        f.write(
            f"{pro_dir}, thr_view={avg_throughput}, lat_view={avg_latency}, "
            f"e2e_reply_tps={avg_e2e_reply_tps}, e2e_lat_avg={avg_e2e_lat_avg_ms}, "
            f"e2e_lat_p50={avg_e2e_lat_p50_ms}, e2e_lat_p95={avg_e2e_lat_p95_ms}, "
            f"e2e_lat_p99={avg_e2e_lat_p99_ms}, good={good_values}/{repeats}, "
            f"fault_node={dead_node_id}, kill_after={kill_after_sec},\n"
        )
    print(
        pro_dir,
        "thr_view=",
        avg_throughput,
        "lat_view=",
        avg_latency,
        "e2e_reply_tps=",
        avg_e2e_reply_tps,
        "e2e_p95=",
        avg_e2e_lat_p95_ms,
        "e2e_p99=",
        avg_e2e_lat_p99_ms,
    )




#conduct a experiment
def experiment(
    protocol,
    debug,
    batchsize,
    payload,
    faults,
    totaltee,
    pct,
    *,
    num_views=10,
    num_cl_trans=1,
    num_clients=1,
    cl_sleep_us=50,
    local_opdist=0,
    client_rep=0,
    kv_set=None,
    kv_get=None,
    kv_del=None,
    kv_keys=None,
    kv_vlen=None,
):
    """
    Remote cluster run aligned with experiment_local(): Redis per replica, server argv includes
    opdist, client argv includes KV workload params (defaults follow module-level kv_* globals).
    """
    kv_set = kv_set_ratio if kv_set is None else kv_set
    kv_get = kv_get_ratio if kv_get is None else kv_get
    kv_del = kv_del_ratio if kv_del is None else kv_del
    kv_keys = kv_keyspace if kv_keys is None else kv_keys
    kv_vlen = kv_value_len if kv_vlen is None else kv_vlen

    pro_dir = f'{protocol}_{faults}_{totaltee}_{payload}_{batchsize}_{pct}'

    factor = protocol_factor(protocol)
    total = num_replicas(factor, faults)

    #get the ip list of servers
    servers = read_servers(total, addresses)
    ips_set = set()
    for server in servers:
        ips_set.add(server[1])
    ips = list(ips_set)

    #scp files to the servers
    if debug:
        files_to_copy = [
            str(PROJECT_ROOT / "config"),
            str(PROJECT_ROOT / "server"),
            str(PROJECT_ROOT / "client"),
        ]
    else:
        files_to_copy = [
            str(PROJECT_ROOT / "config"),
            str(PROJECT_ROOT / "sgxserver"),
            str(PROJECT_ROOT / "sgxclient"),
            str(PROJECT_ROOT / "enclave.so"),
            str(PROJECT_ROOT / "enclave.signed.so"),
            str(PROJECT_ROOT / "sgxkeys"),
        ]
    rm_local_stats()
    scp_files_to_nodes(ips, files_to_copy)

    # Same ordering as experiment_local: Redis before replicas.
    start_remote_redis_for_servers(servers)

    # Multi-threaded SSH execution sgxserver (num_views / opdist match local server argv)
    completion_set, lock = start_all_sgxservers(
        servers,
        debug,
        factor,
        faults,
        totaltee,
        num_views,
        local_opdist,
    )
    print("start")
    wait_before_client = 5 + int(math.ceil(math.log(faults, 2)))
    time.sleep(wait_before_client)
    client_procs = []
    for client_id in range(num_clients):
        client_proc = local_exec_client(
            debug,
            factor,
            faults,
            totaltee,
            num_cl_trans,
            client_id,
            cl_sleep_us,
            client_rep,
            kv_set,
            kv_get,
            kv_del,
            kv_keys,
            kv_vlen,
        )
        client_procs.append(client_proc)

    # Block and wait for all sgxserver instances to end
    wait_for_all_sgxservers_to_finish(completion_set, lock, total)

    # Wait for orchestrator-local client so stats/client-e2e-* is flushed before SCP/parse.
    wait_local_client_procs(client_procs, float(timeoutTime))
    close_client_log_handles(client_procs)

    # get data from nodes
    scp_stats_from_nodes(ips, str(PROJECT_ROOT) + os.sep, str(stats_dir) + os.sep)
    clear_local_out_logs()
    scp_out_logs_from_nodes(ips, str(out_dir))

    stats_directory = str(stats_dir) + os.sep

    # Calculate the average value of the first and second numbers of all vals files
    r1, r2 = calculate_mean_of_values(stats_directory)
    # rtt = find_first_number(stats_directory)

    cp_client_stats()

    e2e_stats = parse_client_e2e_stats(stats_directory)
    if e2e_stats is not None:
        print(
            "cloud client e2e:",
            "reply_ktps=",
            e2e_stats["reply_throughput_ktps"],
            "avg_ms=",
            e2e_stats["e2e_latency_avg_ms"],
            "files=",
            int(e2e_stats["num_client_e2e_files"]),
        )
        er = e2e_stats["reply_throughput_ktps"]
        ea = e2e_stats["e2e_latency_avg_ms"]
    else:
        print("[warn] no stats/client-e2e-* parsed; rebuild sgxclient with Client e2e output or check client logs")
        er = ea = 0.0

    print(
        pro_dir,
        "server_vals_thr_mean=",
        r1,
        "server_vals_lat_mean=",
        r2,
        "e2e_reply_tps=",
        er,
        "e2e_lat_avg_ms=",
        ea,
    )

    with open(stats_txt, 'a') as f:
        if e2e_stats is not None:
            f.write(
                f"{pro_dir}, server_vals_thr_mean={r1}, server_vals_lat_mean={r2}, "
                f"e2e_reply_tps={er}, e2e_lat_avg_ms={ea},\n"
            )
        else:
            f.write(
                f"{pro_dir}, server_vals_thr_mean={r1}, server_vals_lat_mean={r2}, "
                f"e2e_reply_tps=0, e2e_lat_avg_ms=0, e2e_missing=1,\n"
            )


    # Wait and execute sgxclient on the host with id:0
    # ssh_exec_client_on_id0(servers, extra_params, totaltee)

    # Multi-threaded SCP from node to local
    # scp_files_from_nodes(ip_list)o



def main():
    parser = argparse.ArgumentParser(description='Start one experiment with given parameters.')
    parser.add_argument("--p0",        action="store_true",    help="run HybridTEE")
    parser.add_argument("--p01",       action="store_true",    help="run Chianed-HybridTEE")
    parser.add_argument("--p1",        action="store_true",    help="run Achilles")
    parser.add_argument("--p2",        action="store_true",    help="run FlexiBFT")
    parser.add_argument("--p3",        action="store_true",    help="run Damysus")
    parser.add_argument("--p4",        action="store_true",    help="run Oneshot")
    parser.add_argument("--p5",        action="store_true",    help="run hotstuff")
    parser.add_argument("--debug",     action="store_true",    help="non_TEE")
    parser.add_argument("--local",     action="store_true",    help="run locally")
    parser.add_argument('--batchsize', type=int,  default=400, help='MAX_NUM_TRANSACTIONS in params (compile-time batch capacity)')
    parser.add_argument('--payload',   type=int,  default=256, help='Payload size')
    parser.add_argument('--faults',    type=int,  default=1,   help='Number of faults')
    parser.add_argument('--totaltee',  type=int,  default=0,   help='Number of TEE nodes')
    parser.add_argument('--pct',       type=int,  default=0,   help='counter delay')
    # Local experiment parity with experiments.py (execute/computeAvgStats)
    parser.add_argument('--views',type=int,default=10,help='numViews passed to each server (default 10, same as experiments.py)',)
    parser.add_argument('--cl-trans',type=int,default=1,dest='cl_trans',help='number of transactions sent by each client (default 1)',)
    parser.add_argument('--cl-num',type=int,default=1,dest='cl_num',help='number of clients to start (default 1)',)
    parser.add_argument('--cl-sleep',type=int,default=0,dest='cl_sleep',help='client sleep interval in microseconds between sends (default 0)',)
    parser.add_argument('--repeats',type=int,default=1,help='repeat local run and average stats (like experiments --repeats)',)
    parser.add_argument('--config-by-totaltee',action='store_true',help='deprecated: now default behavior; local config already follows --totaltee',)
    parser.add_argument('--config-all-tee',action='store_true',help='legacy mode: force local config to all isTEE:1',)
    parser.add_argument('--opdist',type=int,default=0,help='server argv opdist (default 0, same as experiments.py)',)
    parser.add_argument('--print-vals-mean',action='store_true',help='also print calculate_mean_of_values (first two cols) after each repeat',)
    parser.add_argument('--cutoff-sec',type=int,default=None,help='max local wait time in seconds before forced stop (default uses built-in cutOffBound=60)',)
    parser.add_argument('--kv-set-ratio', type=int, default=30, help='KV workload SET ratio (percentage-like weight)')
    parser.add_argument('--kv-get-ratio', type=int, default=60, help='KV workload GET ratio (percentage-like weight)')
    parser.add_argument('--kv-del-ratio', type=int, default=10, help='KV workload DEL ratio (percentage-like weight)')
    parser.add_argument('--kv-keyspace', type=int, default=1000, help='KV workload keyspace size')
    parser.add_argument('--kv-value-len', type=int, default=16, help='KV workload SET value length')
    # Local fault injection (one replica crash during local experiment).
    parser.add_argument('--fault-local',action='store_true',help='simulate one replica crash during local run (kills a server process mid-run)',)
    parser.add_argument('--fault-node-id',type=int,default=1,help='replica index to kill in --fault-local mode (default 1)',)
    parser.add_argument('--fault-after-sec', type=float, default=2.0, help='seconds after starting the client to kill the fault node (default 2.0)',)
    args = parser.parse_args()

    global kv_set_ratio, kv_get_ratio, kv_del_ratio, kv_keyspace, kv_value_len
    kv_set_ratio = max(0, args.kv_set_ratio)
    kv_get_ratio = max(0, args.kv_get_ratio)
    kv_del_ratio = max(0, args.kv_del_ratio)
    kv_keyspace = max(1, args.kv_keyspace)
    kv_value_len = max(1, args.kv_value_len)

    with open(stats_txt, 'a') as f:
        f.write(f"Start, numviews: {args.views}\n")

    if args.p0:
        Protocol = "HybridTEE"
    elif args.p01:
        Protocol = "Chained-HybridTEE"
    elif args.p1:
        Protocol = "Achilles"
    elif args.p2:
        Protocol = "FlexiBFT"
    elif args.p3:
        Protocol = "Damysus"
    elif args.p4:
        Protocol = "Oneshot"
    elif args.p5:
        Protocol = "Hotstuff"
    else:
        Protocol = "HybridTEE"

    # if args.pct > 0:
    #     pct = args.pct

    factor = protocol_factor(Protocol)
    totalnodes = num_replicas(factor, args.faults)
    # Local mode always regenerates `config` via genLocalConf(...), so skip mkConfig here.
    # This avoids an intermediate config written with mkConfig's instance-rounding policy.
    if not args.local:
        mkConfig(totalnodes, args.totaltee)
    makeInstance(Protocol, args.debug, args.batchsize, args.payload, args.faults, args.pct)

    if args.local:
        if getattr(args, "fault_local", False):
            experiment_fault_local(
                Protocol,
                args.debug,
                args.batchsize,
                args.payload,
                args.faults,
                args.totaltee,
                args.pct,
                dead_node_id=args.fault_node_id,
                kill_after_sec=args.fault_after_sec,
                num_views=args.views,
                num_cl_trans=args.cl_trans,
                num_clients=max(1, args.cl_num),
                cl_sleep_us=max(0, args.cl_sleep),
                repeats=args.repeats,
                config_by_totaltee=not args.config_all_tee,
                local_opdist=args.opdist,
                print_vals_means=args.print_vals_mean,
                cutoff_sec=args.cutoff_sec,
            )
        else:
            experiment_local(
                Protocol,
                args.debug,
                args.batchsize,
                args.payload,
                args.faults,
                args.totaltee,
                args.pct,
                num_views=args.views,
                num_cl_trans=args.cl_trans,
                num_clients=max(1, args.cl_num),
                cl_sleep_us=max(0, args.cl_sleep),
                repeats=args.repeats,
                config_by_totaltee=not args.config_all_tee,
                local_opdist=args.opdist,
                print_vals_means=args.print_vals_mean,
                cutoff_sec=args.cutoff_sec,
            )
    else:
        experiment(
            Protocol,
            args.debug,
            args.batchsize,
            args.payload,
            args.faults,
            args.totaltee,
            args.pct,
            num_views=args.views,
            num_cl_trans=args.cl_trans,
            num_clients=max(1, args.cl_num),
            cl_sleep_us=max(0, args.cl_sleep),
            local_opdist=args.opdist,
        )


if __name__ == "__main__":
    
    main()

