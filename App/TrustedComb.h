#ifndef TRUSTEDCOMB_H
#define TRUSTEDCOMB_H


#include "Hash.h"
#include "Just.h"
#include "Accum.h"
#include "Block.h"
#include "../Enclave/user_types.h"


class TrustedComb {

 private:
  Hash   preph;          // hash of the last prepared block
  View   prepv;          // preph's view
  Hash   lockh;          // hash of the latest locked block
  View   lockv;          // lockh's view
  View   view;           // current view
  Phase1 phase;          // current phase
  PID    id;             // unique identifier
  KEY    priv;           // private key
  unsigned int qsize;    // quorum size
  unsigned int tqsize;   // tee quorum size

  Just sign(Hash h1, Hash h2, View v2);
  void increment();

 public:
  TrustedComb();
  TrustedComb(unsigned int id, KEY priv, unsigned int q, unsigned int tq);

  Just TEEsign();
  Just TEEprepare(Stats &stats, Nodes nodes, Block block, Accum acc, newviews_t proof);
  Just TEEstore(Stats &stats, Nodes nodes, Just just);
  Accum TEEaccum(Stats &stats, Nodes nodes, Just justs[MAX_NUM_SIGNATURES]);
  Accum TEEaccumSp(Stats &stats, Nodes nodes, just_t just);
};


#endif
