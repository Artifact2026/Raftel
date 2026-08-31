#include <stdio.h>
#include <sys/socket.h>
#include <stdlib.h>
#include <netinet/in.h>
#include <string.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <iostream>
#include <fstream>
#include <algorithm>
#include <cmath>
#include <ctime>
#include <random>

#include "Message.h"
#include "KVApp.h"
//#include "Nodes.h"


// Salticidae related stuff
#include "salticidae/msg.h"
#include "salticidae/event.h"
#include "salticidae/network.h"
#include "salticidae/stream.h"


using MsgNet    = salticidae::MsgNetwork<uint8_t>;
using Clock     = std::chrono::time_point<std::chrono::steady_clock>;
using TransInfo = std::tuple<unsigned int,Clock,Transaction>; // int: number of replies

#ifdef COMB
const uint8_t MsgNewViewComb::opcode;
const uint8_t MsgLdrPrepareComb::opcode;
const uint8_t MsgPrepareComb::opcode;
const uint8_t MsgPreCommitComb::opcode;
#elif defined(ACCUM)
const uint8_t MsgNewViewAcc::opcode;
const uint8_t MsgLdrPrepareAcc::opcode;
const uint8_t MsgPrepareAcc::opcode;
const uint8_t MsgPreCommitAcc::opcode;
#else
const uint8_t MsgNewView::opcode;
const uint8_t MsgPrepare::opcode;
const uint8_t MsgLdrPrepare::opcode;
const uint8_t MsgPreCommit::opcode;
const uint8_t MsgCommit::opcode;
#endif

const uint8_t MsgTransaction::opcode;
const uint8_t MsgReply::opcode;
const uint8_t MsgStart::opcode;
//const uint8_t MsgStop::opcode;


salticidae::EventContext ec;
std::unique_ptr<MsgNet> net;          // network
std::map<PID,MsgNet::conn_t> conns;   // connections to the replicas
std::thread send_thread;              // thread to send messages
unsigned int qsize = 0;
unsigned int replyQuorum = 0;      // client completion threshold, not consensus QT
unsigned int numNodes = 0;
unsigned int numFaults = 1;
unsigned int constFactor = 3;         // default value: by default, there are 3f+1 nodes
CID cid = 0;                          // id of this client
//unsigned int numInstancesPerNode = 1; // by default clients send only one transaction per node
unsigned int numInstances = 1;        // by default clients wait for only 1 instance
unsigned int totaltee = 0;            // total number of TEE nodes
std::map<TID,TransInfo> transactions; // current transactions
std::map<TID,double> execTrans;       // set of executed transaction ids
std::map<TID,Clock> execReplyTimes;   // client-side completion timestamp per transaction
unsigned int sleepTime = 1;           // time the client sleeps between two sends (in microseconds)
unsigned int kvSetRatio = 30;         // percentage
unsigned int kvGetRatio = 60;         // percentage
unsigned int kvDelRatio = 10;         // percentage
unsigned int kvKeySpace = 1000;       // number of keys for random workload
unsigned int kvValueLen = 16;         // value length for SET

std::mt19937 rng;

Clock beginning;

unsigned int inst = 0; // instance number when repeating experiments

std::string statsThroughputLatency;
std::string statsEndToEnd;
std::string debugThroughputLatency;

std::string cnfo() {
  return ("[C" + std::to_string(cid) + "]");
}


void send_start_to_all() {
  // TODO: sign
  Sign sign = Sign();
  MsgStart msg = MsgStart(cid,sign);
  if (DEBUGC) { std::cout << cnfo() << "sending start to all" << KNRM << std::endl; }
  for (auto &p: conns) { net->send_msg(msg, p.second); }
}

/*void send_stop_to_all() {
  // TODO: sign
  Sign sign = Sign();
  MsgStop msg = MsgStop(cid,sign);
  if (DEBUG0) { std::cout << cnfo() << "sending stop to all" << KNRM << std::endl; }
  for (auto &p: conns) { net->send_msg(msg, p.second); }
}*/


unsigned int updTransaction(TID tid) {
  std::map<TID,TransInfo>::iterator it = transactions.find(tid);
  unsigned int numReplies = 0;
  if (it != transactions.end()) {
    TransInfo tup = it->second;
    numReplies = std::get<0>(tup)+1;
    transactions[tid]=std::make_tuple(numReplies,std::get<1>(tup),std::get<2>(tup));
  }
  return numReplies;
}

bool compare_double (const double& first, const double& second) { return (first < second); }

double percentile_from_sorted(const std::vector<double> &sorted, double p) {
  if (sorted.empty()) { return 0.0; }
  if (p <= 0.0) { return sorted.front(); }
  if (p >= 100.0) { return sorted.back(); }
  double rank = (p / 100.0) * static_cast<double>(sorted.size() - 1);
  size_t lo = static_cast<size_t>(std::floor(rank));
  size_t hi = static_cast<size_t>(std::ceil(rank));
  if (lo == hi) { return sorted[lo]; }
  double w = rank - static_cast<double>(lo);
  return sorted[lo] * (1.0 - w) + sorted[hi] * w;
}

void printStats() {
  auto end = std::chrono::steady_clock::now();
  double time = std::chrono::duration_cast<std::chrono::microseconds>(end - beginning).count();
  double secs = time / (1000*1000);
  double kops = (numInstances * 1.0) / 1000;
  double throughput = kops/secs;
  if (DEBUGC) std::cout << KMAG << cnfo() << "numInstances=" << numInstances << ";Kops=" << kops << ";secs=" << secs << KNRM << std::endl;

  // we gather all latencies in a list
  std::list<double> allLatencies;
  for (std::map<TID,double>::iterator it = execTrans.begin(); it != execTrans.end(); ++it) {
    if (DEBUGC) std::cout << KMAG << cnfo() << "tid=" << it->first << ";time=" << it->second << KNRM << std::endl;
    double ms = (it->second)/1000; /* latency in milliseconds */
    allLatencies.push_back(ms);
  }

  // we remove the top & bottom 10%
  /*allLatencies.sort(compare_double);
  unsigned int numEntries = execTrans.size();
  unsigned int quant = numEntries / 10;
  for (int i = 0; i < quant; i++) { allLatencies.pop_back(); allLatencies.pop_front(); }*/

  double avg = 0.0;
  std::vector<double> latencySamples;
  for (std::list<double>::iterator it = allLatencies.begin(); it != allLatencies.end(); ++it) {
    avg += (double)*it;
    latencySamples.push_back((double)*it);
  }
  double latency = avg / allLatencies.size(); /* avg of milliseconds spent on a transaction */
  if (DEBUGC) std::cout << KMAG << cnfo() << "latency=" << latency << KNRM << std::endl;
  std::sort(latencySamples.begin(), latencySamples.end());
  double p50 = percentile_from_sorted(latencySamples, 50.0);
  double p95 = percentile_from_sorted(latencySamples, 95.0);
  double p99 = percentile_from_sorted(latencySamples, 99.0);

  Clock firstReply = end;
  Clock lastReply = beginning;
  if (!execReplyTimes.empty()) {
    auto firstIt = std::min_element(execReplyTimes.begin(), execReplyTimes.end(),
      [](const std::pair<const TID,Clock> &a, const std::pair<const TID,Clock> &b) {
        return a.second < b.second;
      });
    auto lastIt = std::max_element(execReplyTimes.begin(), execReplyTimes.end(),
      [](const std::pair<const TID,Clock> &a, const std::pair<const TID,Clock> &b) {
        return a.second < b.second;
      });
    firstReply = firstIt->second;
    lastReply = lastIt->second;
  }
  double replyWindowUs = std::chrono::duration_cast<std::chrono::microseconds>(lastReply - firstReply).count();
  double replyWindowSec = (replyWindowUs <= 0.0) ? secs : (replyWindowUs / 1000.0 / 1000.0);
  if (replyWindowSec <= 0.0) { replyWindowSec = secs; }
  if (replyWindowSec <= 0.0) { replyWindowSec = 1e-9; }
  double replyThroughputKtps = (static_cast<double>(execTrans.size()) / replyWindowSec) / 1000.0;

  std::ofstream f(statsThroughputLatency);
  f << std::to_string(throughput) << " " << std::to_string(latency);
  f.close();

  std::ofstream e(statsEndToEnd);
  e << "reply_throughput_ktps " << replyThroughputKtps << "\n";
  e << "e2e_latency_avg_ms " << latency << "\n";
  e << "e2e_latency_p50_ms " << p50 << "\n";
  e << "e2e_latency_p95_ms " << p95 << "\n";
  e << "e2e_latency_p99_ms " << p99 << "\n";
  e << "duration_total_sec " << secs << "\n";
  e << "duration_reply_window_sec " << replyWindowSec << "\n";
  e << "num_completed " << execTrans.size() << "\n";
  e.close();

  if (DEBUGC) { std::cout << KMAG << cnfo() << "#before=" << execTrans.size() << ";#after=" << allLatencies.size() << KNRM << std::endl; }

  // std::string highLatencies;
  // for (std::map<TID,double>::iterator it = execTrans.begin(); it != execTrans.end(); ++it) {
  //   if (DEBUGC) std::cout << KMAG << cnfo() << "tid=" << it->first << ";time=" << it->second << KNRM << std::endl;
  //   double ms = (it->second)/1000; /* latency in milliseconds */
  //   if (ms > latency) { highLatencies += " " + std::to_string(ms); }
  // }
  // std::ofstream d(debugThroughputLatency, std::ios_base::app);
  // d << "sleepTime=" << std::to_string(sleepTime)
  //   << ";instance=" << std::to_string(inst)
  //   << ";latency="  << std::to_string(latency) << "\n"
  //   << highLatencies << "\n\n";
  // d.close();
}


void executed(TID tid) {
  std::map<TID,TransInfo>::iterator it = transactions.find(tid);
  if (it != transactions.end()) {
    TransInfo tup = it->second;
    auto start = std::get<1>(tup);
    auto end = std::chrono::steady_clock::now();
    double time = std::chrono::duration_cast<std::chrono::microseconds>(end - start).count();
    execTrans[tid]=time;
    execReplyTimes[tid]=end;
  }
}



void handle_reply(MsgReply &&msg, const MsgNet::conn_t &conn) {
  TID tid = msg.tid;
  if (execTrans.find(tid) == execTrans.end()) { // the transaction hasn't been executed yet
    if (DEBUGC) {
      std::cout << cnfo()
                << " reply tid=" << tid
                << " status=" << static_cast<int>(msg.status)
                << " del=" << msg.delCount
                << " value=" << msg.valueStr()
                << KNRM << std::endl;
    }
    unsigned int numReplies = updTransaction(tid);
    if (DEBUGC) { std::cout << cnfo() << "received " << numReplies << "/" << replyQuorum << " replies for transaction " << tid << KNRM << std::endl; }
    if (numReplies == replyQuorum) {
      std::map<TID,TransInfo>::iterator it = transactions.find(tid);
      if (it != transactions.end()) {
        Transaction tx = std::get<2>(it->second);
        AppRequest req;
        if (KVAppCodec::decode(tx, req) && req.op == OpType::OP_GET) {
          if (DEBUGC) {
            std::cout << cnfo() << " GET result tid=" << tid << " key=" << req.key
                      << " status=" << static_cast<int>(msg.status)
                      << " value=" << msg.valueStr() << KNRM << std::endl;
          }
        }
      }
      if (DEBUGC) { std::cout << cnfo() << "received all " << numReplies << " replies for transaction " << tid << KNRM << std::endl; }
      executed(tid);

      if (DEBUGC) { std::cout << cnfo() << "received:" << execTrans.size() << "/" << numInstances << KNRM << std::endl; }
      //if (execTrans.size()%1000 == 0) { std::cout << cnfo() << "received:" << execTrans.size() << "/" << numInstances << KNRM << std::endl; }
      if (execTrans.size() == numInstances) {
        if (DEBUG0) { std::cout << cnfo() << "received replies for all " << numInstances << " transactions...stopping..." << KNRM << std::endl; }
        printStats();
        // Once we have received all the replies we want, we stop by:
        // (1) waiting for the sending thread to finish
        // (2) and by stopping the ec
        send_thread.join();
        //send_stop_to_all();
        ec.stop();
      }
    }
  }
}


void addNewTransaction(Transaction trans) {
  // 0 answers so far
  auto start = std::chrono::steady_clock::now();
  transactions[trans.getTid()]=std::make_tuple(0,start,trans);
}

MsgTransaction mkTransaction(TID transid) {
  std::uniform_int_distribution<unsigned int> ratioDist(1, 100);
  std::uniform_int_distribution<unsigned int> keyDist(0, std::max(1U, kvKeySpace) - 1);
  std::uniform_int_distribution<int> chDist(0, 25);

  unsigned int r = ratioDist(rng);
  OpType op = OpType::OP_GET;
  if (r <= kvSetRatio) { op = OpType::OP_SET; }
  else if (r <= kvSetRatio + kvGetRatio) { op = OpType::OP_GET; }
  else { op = OpType::OP_DEL; }

  AppRequest req;
  req.op = op;
  req.client_id = static_cast<int>(cid);
  req.req_id = static_cast<int>(transid);
  req.key = "k" + std::to_string(keyDist(rng));
  if (op == OpType::OP_SET) {
    req.value.reserve(kvValueLen);
    for (unsigned int i = 0; i < kvValueLen; ++i) {
      req.value.push_back(static_cast<char>('a' + chDist(rng)));
    }
  }

  std::array<unsigned char, PAYLOAD_SIZE> payload;
  bool encoded = KVAppCodec::encode(req, payload);
  Transaction transaction = encoded
      ? Transaction(cid, transid, payload)
      : Transaction(cid, transid, static_cast<char>(17));
  addNewTransaction(transaction);
  // TODO: sign
  Sign sign = Sign();
  MsgTransaction msg(transaction,sign);
  return msg;
}

void send_one_trans(MsgTransaction msg, MsgNet::conn_t conn) {
  net->send_msg(msg,conn);
}

unsigned int send_transactions_to_all(TID transid) {
  MsgTransaction msg = mkTransaction(transid);
  for (auto &p: conns) {
    if (DEBUGC) { std::cout << cnfo() << "sending transaction(" << transid << ") to " << p.first << KNRM << std::endl; }
    net->send_msg(msg, p.second);
    //std::thread thSend(send_one_trans,msg,p.second);
  }
  return transid + 1;
}


// void old_send_transactions() {
//   TID transid = 1; // The transaction id '0' is reserved for dummy transactions
//   beginning = std::chrono::steady_clock::now();
//   for (unsigned int counter = 0; counter < numInstancesPerNode; counter++) {
//     transid = send_transactions_to_all(transid);
//     usleep(sleepTime);
//     if (DEBUGC) { std::cout << cnfo() << "slept for " << sleepTime << "; counter=" << counter << KNRM << std::endl; }
//   }
// }


void send_transactions() {
  // The transaction id '0' is reserved for dummy transactions
  unsigned int transid = 1;
  beginning = std::chrono::steady_clock::now();

  // Build each transaction once and disseminate the same request to every
  // replica.  Leadership rotates across all replicas, so restricting client
  // requests to TEE replicas can leave the current leader without the request.
  while (transid <= numInstances) {
    MsgTransaction msg = mkTransaction(transid);
    for (auto &p: conns) {
      if (DEBUGC) { std::cout << cnfo() << "broadcasting transaction(" << transid << ") to node " << p.first << KNRM << std::endl; }
      net->send_msg(msg, p.second);
    }
    transid++;
    usleep(sleepTime);
  }
}


int main(int argc, char const *argv[]) {
  KeysFun kf;

  if (argc > 1) { sscanf(argv[1], "%d", &cid); }
  std::cout << "[C-id=" << cid << "]" << KNRM << std::endl;

  if (argc > 2) { sscanf(argv[2], "%d", &numFaults); }
  std::cout << cnfo() << "#faults=" << numFaults << KNRM << std::endl;

  if (argc > 3) { sscanf(argv[3], "%d", &constFactor); }
  std::cout << cnfo() << "constFactor=" << constFactor << KNRM << std::endl;

  if (argc > 4) { sscanf(argv[4], "%d", &numInstances); }
  std::cout << cnfo() << "numInstances=" << numInstances << KNRM << std::endl;

  if (argc > 5) { sscanf(argv[5], "%d", &sleepTime); }
  std::cout << cnfo() << "sleepTime=" << sleepTime << KNRM << std::endl;

  if (argc > 6) { sscanf(argv[6], "%d", &inst); }
  std::cout << cnfo() << "instance=" << inst << KNRM << std::endl;

  if (argc > 7) { sscanf(argv[7], "%d", &totaltee); }
  std::cout << cnfo() << "totaltee=" << totaltee << KNRM << std::endl;

  // Optional KV workload params:
  // argv[8]=set ratio, argv[9]=get ratio, argv[10]=del ratio, argv[11]=keyspace, argv[12]=value length.
  if (argc > 8) { sscanf(argv[8], "%d", &kvSetRatio); }
  if (argc > 9) { sscanf(argv[9], "%d", &kvGetRatio); }
  if (argc > 10) { sscanf(argv[10], "%d", &kvDelRatio); }
  if (argc > 11) { sscanf(argv[11], "%d", &kvKeySpace); }
  if (argc > 12) { sscanf(argv[12], "%d", &kvValueLen); }
  unsigned int ratioSum = kvSetRatio + kvGetRatio + kvDelRatio;
  if (ratioSum == 0) {
    kvSetRatio = 30; kvGetRatio = 60; kvDelRatio = 10; ratioSum = 100;
  }
  // Normalize to 100 to keep distribution logic simple.
  kvSetRatio = (kvSetRatio * 100) / ratioSum;
  kvGetRatio = (kvGetRatio * 100) / ratioSum;
  kvDelRatio = 100 - kvSetRatio - kvGetRatio;
  if (kvKeySpace == 0) { kvKeySpace = 1; }
  if (kvValueLen == 0) { kvValueLen = 1; }

  std::seed_seq seed{
    static_cast<unsigned int>(cid),
    static_cast<unsigned int>(inst),
    static_cast<unsigned int>(numInstances),
    static_cast<unsigned int>(time(nullptr)),
  };
  rng.seed(seed);


  numNodes = (constFactor*numFaults)+1;
  qsize = numNodes-numFaults;
  replyQuorum = numFaults+1;
  std::string confFile = "config";
  Nodes nodes(confFile,numNodes);
  //numInstancesPerNode = numInstances / numNodes;
  //numInstances = numInstancesPerNode * numNodes;
  //std::cout << cnfo() << "instances per node:" << numInstancesPerNode << "; instances:" << numInstances << KNRM << std::endl;

  // -- Stats
  auto timeNow = std::chrono::system_clock::now();
  std::time_t time = std::chrono::system_clock::to_time_t(timeNow);
  struct tm y2k = {0};
  double seconds = difftime(time,mktime(&y2k));
  std::string stamp = std::to_string(inst) + "-" + std::to_string(cid) + "-" + std::to_string(seconds);
  statsThroughputLatency = "stats/client-throughput-latency-" + stamp;
  statsEndToEnd = "stats/client-e2e-" + stamp;
  debugThroughputLatency = "stats/debug-client-throughput-latency";


  // std::ofstream d("debug", std::ios_base::app);
  // d << "sleepTime=" << std::to_string(sleepTime) << ";instance=" << std::to_string(inst) << "\n";
  // d.close();


  // -- Public keys
  for (unsigned int i = 0; i < numNodes; i++) {
    //public key
    KEY pub;
    // Set public key - nothing special to do for EC
#if defined(KK_RSA4096) || defined(KK_RSA2048)
    pub = RSA_new();
#endif
#if (defined(ACCUM) || defined(COMB)) && defined(KK_EC256)
    BIO *bio = BIO_new(BIO_s_mem());
    int w = BIO_write(bio,pub_key256,sizeof(pub_key256));
    pub = PEM_read_bio_EC_PUBKEY(bio, NULL, NULL, NULL);
#else
    kf.loadPublicKey(i,&pub);
#endif
    if (DEBUGC) std::cout << KMAG << "node id: " << i << KNRM << std::endl;
    nodes.setPub(i,pub);
  }


  long unsigned int size = std::max({sizeof(MsgTransaction), sizeof(MsgReply), sizeof(MsgStart)});

  #if defined(BASIC_HYBRID_TEE) || defined(BASIC_HYBRID_TEE_DEBUG)
  size = std::max({size,
                   sizeof(MsgNewViewComb),
                   sizeof(MsgLdrPrepareComb),
                   sizeof(MsgPrepareComb)});
  #elif defined(CHAINED_HYBRID_TEE) || defined(CHAINED_HYBRID_TEE_DEBUG)
  size = std::max({size,
                   sizeof(MsgNewViewChComb),
                   sizeof(MsgLdrPrepareChComb),
                   sizeof(MsgPrepareChComb)});
  #elif defined(BASIC_HOTSTUFF)
  size = std::max({size,
                   sizeof(MsgNewView),
                   sizeof(MsgLdrPrepare),
                   sizeof(MsgPrepare),
                   sizeof(MsgPreCommit),
                   sizeof(MsgCommit)});
  #else
  #error "Unsupported protocol macro for Client.cpp"
  #endif

  MsgNet::Config config;
  config.max_msg_size(size);
  //config.ping_period(2);
  net = std::make_unique<MsgNet>(ec,config);

  //salticidae::NetAddr addr = salticidae::NetAddr("127.0.0.1:" + std::to_string(8760 + numNodes));
  net->start();
  //net.listen(addr);

  std::cout << cnfo() << "connecting..." << KNRM << std::endl;
  for (size_t j = 0; j < numNodes; j++) {
    NodeInfo* othernode = nodes.find(j);
    if (othernode != NULL) {
      std::cout << cnfo() << "connecting to " << j << KNRM << std::endl;
      salticidae::NetAddr peer_addr(othernode->getHost() + ":" + std::to_string(othernode->getCPort()));
      conns.insert(std::make_pair(j,net->connect_sync(peer_addr)));
    } else {
      std::cout << KLRED << cnfo() << "couldn't find " << j << "'s information among nodes" << KNRM << std::endl;
    }
  }

  net->reg_handler(handle_reply);

  send_start_to_all();
  send_thread = std::thread([]() { send_transactions(); });

  auto shutdown = [&](int) {ec.stop();};
  salticidae::SigEvent ev_sigterm(ec, shutdown);
  ev_sigterm.add(SIGTERM);

  std::cout << cnfo() << "dispatch" << KNRM << std::endl;
  ec.dispatch();

  return 0;
}
