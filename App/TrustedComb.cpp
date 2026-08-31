#include <stdio.h>
#include <stdlib.h>
#include <iostream>
#include <string>
#include <cstring>

#include "TrustedComb.h"

namespace {
bool verifyQuorum(Stats &stats, PID verifier, Nodes nodes, RData data,
                  Signs signs, unsigned int qsize, unsigned int tqsize) {
  if ((signs.getSize() != tqsize && signs.getSize() != qsize) || signs.getSize() == 0
      || signs.getSize() > MAX_NUM_SIGNATURES) {
    return false;
  }
  std::set<PID> unique;
  bool allTEE = true;
  for (unsigned int i = 0; i < signs.getSize(); ++i) {
    Sign sign = signs.get(i);
    NodeInfo *node = nodes.find(sign.getSigner());
    if (!sign.isSet() || node == NULL || !unique.insert(sign.getSigner()).second) {
      return false;
    }
    allTEE = allTEE && node->getIsTEE();
  }
  if (signs.getSize() != qsize && !(signs.getSize() == tqsize && allTEE)) {
    return false;
  }
  return signs.verify(stats, verifier, nodes, data.toString());
}
}


TrustedComb::TrustedComb() {
  this->preph  = Hash(true); // the genesis block
  this->prepv  = 0;
  this->lockh  = Hash(true);
  this->lockv  = 0;
  this->view   = 1; // view 0 is reserved for the genesis block
  this->phase  = PH1_NEWVIEW;
  this->qsize  = 0;
  this->tqsize  = 0;
}

TrustedComb::TrustedComb(PID id, KEY priv, unsigned int q, unsigned int tq) {
  this->preph  = Hash(true); // the genesis block
  this->prepv  = 0;
  this->lockh  = Hash(true);
  this->lockv  = 0;
  this->view   = 1; // view 0 is reserved for the genesis block
  this->phase  = PH1_NEWVIEW;
  this->id     = id;
  this->priv   = priv;
  this->qsize  = q;
  this->tqsize  = tq;
}


// increments the (view,phase) pair
void TrustedComb::increment() {
  if (this->phase == PH1_NEWVIEW) {
    this->phase = PH1_PREPARE;
  } else if (this->phase == PH1_PREPARE) {
    this->phase = PH1_PRECOMMIT;
  } else if (this->phase == PH1_PRECOMMIT) {
    this->phase = PH1_COMMIT;
  } else if (this->phase == PH1_COMMIT) {
    this->phase = PH1_NEWVIEW;
    this->view++;
  }
}


Just TrustedComb::sign(Hash h1, Hash h2, View v2) {
  RData rdata(h1,this->view,h2,v2,this->phase);
  Sign sign(this->priv,this->id,rdata.toString());
  Just just(rdata,sign);

  increment();

  return just;
}


Just TrustedComb::TEEsign() {
  return sign(Hash(false),this->preph,this->prepv);
}


Just TrustedComb::TEEprepare(Stats &stats, Nodes nodes, Block block, Accum acc, newviews_t proof) {
  Signs signs(acc.getSign());
  std::set<PID> signers;
  View highest = 0;
  Hash highestHash = Hash(true);
  bool allTEE = true;
  bool validProof = proof.size > 0 && proof.size <= MAX_NUM_SIGNATURES
                 && (proof.size == this->qsize || proof.size == this->tqsize);
  for (unsigned int i = 0; validProof && i < proof.size; ++i) {
    onejust_t raw = proof.justs[i];
    RData data(Hash(raw.rdata.proph.set, raw.rdata.proph.hash), raw.rdata.propv,
               Hash(raw.rdata.justh.set, raw.rdata.justh.hash), raw.rdata.justv,
               raw.rdata.phase);
    Sign vote(raw.sign.set, raw.sign.signer, raw.sign.sign);
    NodeInfo *node = nodes.find(raw.sign.signer);
    Signs one(vote);
    validProof = raw.set && node != NULL && vote.isSet()
              && signers.insert(raw.sign.signer).second
              && data.getPhase() == PH1_NEWVIEW
              && data.getPropv() == this->view
              && one.verify(stats, this->id, nodes, data.toString());
    if (node != NULL) { allTEE = allTEE && node->getIsTEE(); }
    if (validProof && data.getJustv() >= highest) {
      highest = data.getJustv();
      highestHash = data.getJusth();
    }
  }
  validProof = validProof
            && (proof.size == this->qsize || (proof.size == this->tqsize && allTEE))
            && acc.getSize() == proof.size
            && acc.getView() == this->view
            && acc.getPrepv() == highest
            && acc.getPreph() == highestHash;
  bool safeParent = block.extends(acc.getPreph())
                 && (block.getPrevHash() == this->lockh || acc.getPrepv() > this->lockv);
  Hash hash = block.hash();
  if (validProof && safeParent
      && signs.verify(stats,this->id,nodes,acc.data2string())) {
    NodeInfo *leader = nodes.find(acc.getSign().getSigner());
    if (leader != NULL && leader->getIsTEE()) {
      this->preph = hash;
      this->prepv = this->view;
    }
    return sign(hash,acc.getPreph(),acc.getPrepv());
  } else {
    if (DEBUG1) std::cout << KMAG << "[" << this->id << "]" << "TEEprepare failed because:"
                          << "verif=" << (signs.verify(stats,this->id,nodes,acc.data2string()))
                          << ";proof=" << validProof
                          << ";safe-parent=" << safeParent
                          << KNRM << std::endl;
  }
  return Just();
}


Just TrustedComb::TEEstore(Stats &stats, Nodes nodes, Just just) {
  RData  data  = just.getRData();
  Signs  signs = just.getSigns();
  Hash   h     = data.getProph();
  View   v     = data.getPropv();
  Phase1 ph    = data.getPhase();
  if (verifyQuorum(stats, this->id, nodes, data, signs, this->qsize, this->tqsize)
      && this->view == v
      && (ph == PH1_PREPARE || ph == PH1_PRECOMMIT)) {
    this->preph=h; this->prepv=v;
    if (ph == PH1_PRECOMMIT) {
      this->lockh=h; this->lockv=v;
    }
    return sign(h,Hash(),View());
  } else {
    if (DEBUG1) std::cout << KMAG << "[" << this->id << "]" << "TEEstore failed because:"
                          << "size="   << (signs.getSize() == this->qsize)
                          << ";verif=" << (signs.verify(stats,this->id,nodes,data.toString()))
                          << ";vierw=" << (this->view == v)
                          << ";phase=" << (ph == PH1_PREPARE || ph == PH1_PRECOMMIT)
                          << KNRM << std::endl;
  }
  return Just();
}


Accum TrustedComb::TEEaccum(Stats &stats, Nodes nodes, Just justs[MAX_NUM_SIGNATURES]) {
  View v = justs[0].getRData().getPropv();
  View highest = 0;
  Hash hash = Hash();
  std::set<PID> signers;

  for (int i = 0; i < MAX_NUM_SIGNATURES && i < this->qsize; i++) {
    Just  just  = justs[i];
    RData data  = just.getRData();
    Signs signs = just.getSigns();
    if (signs.getSize() == 1) {
      Sign sign  = signs.get(0);
      PID signer = sign.getSigner();
      if (data.getPhase() == PH1_NEWVIEW
          && data.getPropv() == v
          && signers.find(signer) == signers.end()
          && signs.verify(stats,this->id,nodes,data.toString())) {
        if (DEBUG1) std::cout << KMAG << "[" << this->id << "]" << "inserting signer" << KNRM << std::endl;
        signers.insert(signer);
        if (data.getJustv() >= highest) {
          highest = data.getJustv();
          hash = data.getJusth();
        }
      }
    }
  }

  bool set = true;
  unsigned int size = signers.size();
  std::string text = std::to_string(set) + std::to_string(v) + std::to_string(highest) + hash.toString() + std::to_string(size);
  Sign sign(this->priv,this->id,text);
  return Accum(v,highest,hash,size,sign);
}


Accum TrustedComb::TEEaccumSp(Stats &stats, Nodes nodes, just_t just) {
  std::set<PID> signers;

  rdata_t rdata = just.rdata;
  signs_t signs = just.signs;
  Hash proph = Hash(rdata.proph.set,rdata.proph.hash);
  View propv = rdata.propv;
  Hash justh = Hash(rdata.justh.set,rdata.justh.hash);
  View justv = rdata.justv;
  Phase1 phase = rdata.phase;
  std::string data = proph.toString() + std::to_string(propv) + justh.toString() + std::to_string(justv) + std::to_string(phase);

  if (phase == PH1_NEWVIEW) {
    for (int i = 0; i < MAX_NUM_SIGNATURES && i < this->qsize && i < signs.size; i++) {
      PID signer = signs.signs[i].signer;
      Signs sign = Sign(signs.signs[i].set,signer,signs.signs[i].sign);
      bool vd = Signs(sign).verify(stats,this->id,nodes,data);
      if (signers.find(signer) == signers.end() && vd) { signers.insert(signer); }
    }
  }

  bool set = true;
  unsigned int size = signers.size();
  std::string text = std::to_string(set) + std::to_string(propv) + std::to_string(justv) + justh.toString() + std::to_string(size);
  Sign sign(this->priv,this->id,text);
  return Accum(propv,justv,justh,size,sign);
}
