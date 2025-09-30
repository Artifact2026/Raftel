# Breaking Fault Lines: Unifying BFT Consensus in a Partially Trusted World

This is the accompanying code to the paper "Breaking Fault Lines: Unifying BFT Consensus in a Partially Trusted World".


## Current status

The software is under ongoing development.

## Description
The main implementation of the project is located in the App and enclave directories. The core consensus logic is implemented in App/Handler.cpp, and the primary functionalities within SGX are implemented in Enclave/EnclaveComb.cpp. We add a macro named BASIC_HYBRID_TEE for our protocol in the code, and its corresponding implementation is guarded by the preprocessor condition #if defined(BASIC_HYBRID_TEE).

## Installing

We use the
[Salticidae](https://github.com/Determinant/salticidae) library, which
is added here as git submodule.

### Salticidae

If you decide to install Salticidae locally, you will need git and cmake.
In which case, after cloning the repository you need to type this to initialize the
Salticidae git submodule:

`git submodule init`

followed by:

`git submodule update`

Salticidea has the following dependencies:

* CMake >= 3.9
* C++14
* libuv >= 1.10.0
* openssl >= 1.1.0

`sudo apt install cmake libuv1-dev libssl-dev`

Then, to instance Salticidae, type:
`(cd salticidae; cmake . -DCMAKE_INSTALL_PREFIX=.; make; make install)`

### Python

We use python version 3.8.10.  You will need python3-pip to install
the required modules.

The Python script relies on the following modules:
- subprocess
- pathlib
- matplotlib
- time
- math
- os
- glob
- datetime
- argparse
- enum
- json
- multiprocessing
- random
- shutil
- re
- scp
- threading

If you haven't installed those modules yet, run:

`python3 -m pip install subprocess pathlib matplotlib time math os glob datetime argparse enum json multiprocessing random shutil re scp threading`

### SGX 
We use SGX SDK 2.23.

followed by:

`bash deployment/init.sh`




## Experiments

### Default command

To tests our protocols, we provide a Python script, called
`run.py`. We explain the various options our Python scripts provides. You will
run commands of the following form, followed by various options
explained below:

`python3 run.py --local --p0`

### Options

In addition, you can use the following options to change some of the parameters:
- `--pall` is to run all protocols, instead you can use `--p1` up to `--p3`
    - `--p0`: Raftel
    - `--p01`: Chained-Raftel
- `--payload n` to change the payload size to `n`
- `--faults n` to change the number of faults to `n`
- `--batchsize n` to change the batch size to `n`
- `--local` is to run the experiment locally



### Local Experiemnts

Use `--local` to conduct local experiments.

For example, if you run:

`python3 run.py  --local --p0  --faults 1 --payload 256 --batchsize 400`

then you will run the replicas locally, test the Achilles (`--p1`), test for number of faults is 1 (`--faults 1`), payload size is 256 (`--payload 256`), and batchsize is 400 (`--batchsize 400`).

The results will be printed directly to the command line. For example, if you see output:
```
all processes are done
throughput-view: 175.84404966666668 out of 3
latency-view: 15.088674666666668 out of 3
```
this indicates that the experiment executed successfully, with an average throughput of 175.84K TPS and an average latency of 15.08 ms across 3 nodes.
