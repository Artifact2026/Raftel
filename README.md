# Breaking Fault Lines: Unifying BFT Consensus in a Partially Trusted World

This repository contains the code accompanying the paper "Breaking Fault Lines: Unifying BFT Consensus in a Partially Trusted World".

## Current status

The software is under ongoing development.

## Description

The main implementation is located in the `App` and `Enclave` directories. The core consensus logic is implemented in `App/Handler.cpp`, and the primary SGX functionality is implemented in `Enclave/EnclaveComb.cpp`. Our protocol uses the `BASIC_HYBRID_TEE` macro, and its implementation is guarded by `#if defined(BASIC_HYBRID_TEE)`.

## Installing

The documented and deployment-tested environment is Ubuntu 20.04 x86-64 with Python 3.8.10. The default build uses Intel SGX simulation mode (`SGX_MODE=SIM`), so SGX-capable hardware is not required for the minimal local test. The SGX SDK and SGX SSL are still required to compile it.

### Required versions

| Dependency | Version used by this project | Purpose |
| --- | --- | --- |
| Ubuntu | 20.04 x86-64 | Reference build and deployment OS |
| Python | 3.8.10 | Experiment driver |
| CMake | 3.9 or newer | Salticidae build |
| C++ compiler | C++14 capable (Ubuntu 20.04 `build-essential`) | Application build |
| libuv | 1.10.0 or newer | Salticidae/networking |
| OpenSSL | 1.1.x (Ubuntu 20.04 provides 1.1.1) | Cryptography |
| `pkg-config` | 0.29.1 (Ubuntu 20.04 package) | Build flag discovery |
| Intel SGX SDK | 2.23.100.2 | Enclave build, including SIM mode |
| Intel SGX SSL | Repository-bundled binary package (no upstream version metadata is included) | Trusted and untrusted SGX SSL libraries |
| Salticidae | Repository source snapshot (no release tag is recorded) | Networking library |
| hiredis | 0.14.0 (Ubuntu 20.04 package) | Redis client library; experiment reproduction only |
| Redis | 5.0.7 (Ubuntu 20.04 package) | Redis server; experiment reproduction only |

The repository does not contain upstream version metadata for its SGX SSL binary package or a Salticidae release tag. For exact reproduction, use the bundled SGX SSL archive and Salticidae source snapshot instead of substituting another release. Ubuntu security updates may add a package revision suffix to the `pkg-config`, hiredis, and Redis versions above without changing their upstream version.

### System packages

Install the packages required by the minimal in-memory test:

```bash
sudo apt-get update
sudo apt-get install -y \
  build-essential cmake git libssl-dev libuv1-dev pkg-config \
  python3 python3-pip
```

The Redis-backed experiment path additionally requires hiredis and Redis:

```bash
sudo apt-get install -y libhiredis-dev redis-server
pkg-config --modversion hiredis
redis-server --version
```

Use `--redis` when running an experiment that should use this backend. The default local test uses the in-memory backend and does not start Redis.

### Python packages

Most modules imported by `run.py` are part of the Python standard library and must not be installed separately. Install only its third-party dependencies:

```bash
python3 -m pip install matplotlib paramiko scp
```

Ali Cloud deployment additionally requires the Aliyun Python SDK:

```bash
python3 -m pip install aliyun-python-sdk-core
```

### Salticidae

Salticidae is included in the repository. If the checkout was cloned with Git submodules, initialize it first:

```bash
git submodule update --init --recursive
```

Build and install Salticidae into its repository-local prefix:

```bash
cmake -S salticidae -B salticidae/build -DCMAKE_INSTALL_PREFIX="$PWD/salticidae"
cmake --build salticidae/build -j"$(nproc)"
cmake --install salticidae/build
```

The project `Makefile` looks for its headers and libraries under `salticidae/include` and `salticidae/lib`.

### Intel SGX SDK and SGX SSL

The build expects the following fixed locations:

```text
/opt/intel/sgxsdk
/opt/intel/sgxssl/include
/opt/intel/sgxssl/lib64
```

The deployment bundle contains the Intel SGX SDK 2.23.100.2 installer and the SGX SSL package. To install the same versions used on the Ali Cloud nodes:

```bash
mkdir -p /tmp/raftel-sgx-install
cd /tmp/raftel-sgx-install
tar -xzf /root/Raftel/deployment/sourcefile/archive.tar.gz
sudo mkdir -p /opt/intel
printf 'no\n/opt/intel\n' | sudo ./sgx_linux_x64_sdk_2.23.100.2.bin
tar -xzf sgxssl.tar.gz
sudo mkdir -p /opt/intel/sgxssl/include /opt/intel/sgxssl/lib64
sudo cp -a package/include/. /opt/intel/sgxssl/include/
sudo cp -a package/lib64/. /opt/intel/sgxssl/lib64/
```

Before compiling or running a test, load the SGX SDK environment:

```bash
source /opt/intel/sgxsdk/environment
export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-}:/opt/intel/sgxsdk/sdk_libs:/usr/local/lib"
```

The commands above install the SDK needed by the default simulation build. Hardware-mode deployment also needs the SGX driver and platform software. The Ali Cloud initialization script installs those components automatically.

## Experiments

`run.py` compiles the selected protocol, generates the local configuration, starts the replicas and client, and prints the aggregated throughput and latency. Run it from the repository root. A run modifies `App/params.h` and creates or updates `config`, `exe/`, `out/`, `stats/`, and `stats.txt`.

### Local experiments

The protocol selectors implemented by `run.py` are:

| Option | Protocol |
| --- | --- |
| `--p0` | HybridTEE (Raftel) |
| `--p01` | Chained-HybridTEE (Chained-Raftel) |
| `--p1` | Achilles |
| `--p5` | Hotstuff |
| `--p6` | Basic-Damysus |

If no protocol selector is supplied, `run.py` defaults to `--p0`. There is no `--pall`, `--p2`, or `--p3` option.

Common local options are:

| Option | Default | Description |
| --- | --- | --- |
| `--local` | disabled | Run all replicas on the current machine |
| `--faults N` | `1` | Number of tolerated faults; the protocol determines the replica count |
| `--totaltee N` | `0` | Number of TEE replicas for HybridTEE; ignored for protocols with a fixed TEE population |
| `--payload N` | `256` | Payload size in bytes |
| `--batchsize N` | `400` | Compile-time maximum transactions per batch |
| `--views N` | `10` | Number of views passed to each replica |
| `--cl-trans N` | `1` | Transactions sent by each client |
| `--cl-num N` | `1` | Number of clients |
| `--cl-sleep N` | `0` | Delay in microseconds between client sends |
| `--repeats N` | `1` | Number of runs to average |
| `--cutoff-sec N` | `60` | Maximum wait time for a local run |
| `--redis` | disabled | Use the Redis-backed KV path instead of the in-memory backend |
| `--debug` | disabled | Build and run the non-enclave server executable |

Run `python3 run.py --help` for fault-injection, leader-selection, workload-mix, and plotting options.

#### Minimal local test

After installing the required dependencies and loading the SGX SDK environment, run:

```bash
cd /root/Raftel
source /opt/intel/sgxsdk/environment
python3 run.py --local --p0 --faults 1 --totaltee 2 \
  --payload 256 --batchsize 400 --views 3 --cl-trans 1 --repeats 1
```

This compiles HybridTEE in SGX simulation mode and runs four local replicas, two of which are configured as TEE replicas. The first compilation can take several minutes. A successful run finishes all processes and prints throughput and latency summaries similar to:

```text
all processes are done
throughput-view: 175.84 out of 3
latency-view: 15.08 out of 3
```

The exact values depend on the machine; successful process completion and non-empty throughput/latency results are the relevant smoke-test criteria.

### Ali Cloud experiments

The deployment scripts currently assume that this repository is checked out at `/root/Raftel` on the coordinator machine. They also assume Ubuntu 20.04 ECS instances, root SSH access, a private network route from the coordinator to every instance, and the SSH private key `/root/Raftel/TShard`.

Before deployment, replace every account- and network-specific value in `deployment/config.json` with values from your Ali Cloud account. In particular, configure the region, access key, image, security group, VPC, vSwitch, instance type, and key-pair name. Do not commit access keys or private keys to the repository.

#### Launch instances

Install the Aliyun SDK, then create the instances from the coordinator:

```bash
cd /root/Raftel/deployment
python3 create_run_instances.py
```

The default `instance_count` in `deployment/config.json` is `7`. Instance IDs are appended to `deployment/instances.txt`; make sure it contains only the instances for the current deployment before continuing. Wait until all instances are running, then obtain their private IP addresses:

```bash
python3 get_priv_ip.py
```

This writes `deployment/priv_ip.txt`. Generate the replica and client address files with:

```bash
python3 gen_ip.py 35 5
```

The first argument is the requested number of replica addresses and the second is the number of addresses assigned to each IP. `gen_ip.py` rounds the first number up to a multiple of the second, so `python3 gen_ip.py 31 5` generates 35 addresses rather than 31. It writes `/root/Raftel/config`, `/root/Raftel/servers`, `/root/Raftel/clients`, and `/root/Raftel/ip_list`.

Transfer the deployment archives and initialization scripts to every address in `deployment/priv_ip.txt`:

```bash
bash cloud_deploy.sh
```

At present, `cloud_deploy.sh` performs the file transfer only; its instance-creation and address-generation commands are commented out. Therefore, the preceding three steps must be run explicitly.

#### Configure the nodes

Start one background tmux setup session per instance:

```bash
bash cloud_config.sh
```

Each session connects to a node and runs `/root/init.sh`. Inspect a particular setup session with:

```bash
tmux list-sessions
tmux attach -t setup1
```

Detach without stopping the remote installation by pressing `Ctrl-b`, then `d`. Do not type `exit` merely to detach: it terminates the SSH shell in that session. When all installations have completed, close the setup sessions with:

```bash
bash close.sh
```

Warning: `close.sh` kills every tmux session on the coordinator, not only sessions named `setup*`.

For Redis-backed reproduction, install hiredis on all configured nodes after the SGX setup:

```bash
bash install_hiredis_on_ips.sh
```

#### Run a cloud experiment

Run `run.py` from `/root/Raftel` without `--local`. It reads the generated node files and deploys the compiled binaries to the remote nodes. For example:

```bash
cd /root/Raftel
source /opt/intel/sgxsdk/environment
python3 run.py --p0 --faults 1 --totaltee 2 \
  --payload 256 --batchsize 400 --views 10 --cl-trans 1
```

Add `--redis` to reproduce the Redis-backed KV path. Remote stdout logs are copied into `out/`, and experiment statistics are collected under `stats/` and `stats.txt`.

Ali Cloud resources incur charges. When the experiment is complete, verify the IDs in `deployment/instances.txt` and release those instances with:

```bash
cd /root/Raftel/deployment
python3 delete_instances.py
```
