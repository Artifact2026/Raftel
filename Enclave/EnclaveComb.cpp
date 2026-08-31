#include <set>
#include "EnclaveShare.h"


hash_t COMBpreph = newHash(); // hash of the last prepared block
View   COMBprepv = 0;             // preph's view
hash_t COMBlockh = newHash(); // hash of the latest locked block
View   COMBlockv = 0;         // lockh's view
View   COMBview  = 1;             // view 0 is reserved for the genesis block
Phase1 COMBphase = PH1_NEWVIEW;   // current phase

std::string COMB_transaction2string(const trans_t &transaction) {
  std::string text = std::to_string(transaction.clientid)
                   + std::to_string(transaction.transid);
  text.append(reinterpret_cast<const char *>(transaction.data), PAYLOAD_SIZE);
  return text;
}

hash_t COMB_hashBlock(const basicblock_t *block) {
  hash_t result = noHash();
  if (!block->set || block->size > MAX_NUM_TRANSACTIONS) {
    return result;
  }
  std::string text = std::to_string(block->id)
                   + std::to_string(block->set)
                   + hash2string(block->prev_hash)
                   + std::to_string(block->size);
  for (unsigned int i = 0; i < block->size; ++i) {
    text += COMB_transaction2string(block->trans[i]);
  }
  result.set = true;
  if (!SHA256(reinterpret_cast<const unsigned char *>(text.data()), text.size(), result.hash)) {
    return noHash();
  }
  return result;
}

bool COMB_validateNewViews(const newviews_t *newviews, accum_t *acc) {
  if (newviews->size == 0 || newviews->size > MAX_NUM_SIGNATURES
      || (newviews->size != getQsize() && newviews->size != getTQsize())) {
    return false;
  }
  bool allTEE = true;
  std::set<PID> signers;
  View highest = 0;
  hash_t highestHash = newHash();
  for (unsigned int i = 0; i < newviews->size; ++i) {
    onejust_t entry = newviews->justs[i];
    signs_t one;
    one.size = 1;
    one.signs[0] = entry.sign;
    std::map<PID, bool>::const_iterator type = node_is_TEE.find(entry.sign.signer);
    if (!entry.set || type == node_is_TEE.end()
        || !signers.insert(entry.sign.signer).second
        || entry.rdata.phase != PH1_NEWVIEW
        || entry.rdata.propv != COMBview
        || !verifyText(one, rdata2string(entry.rdata))) {
      return false;
    }
    allTEE = allTEE && type->second;
    if (entry.rdata.justv >= highest) {
      highest = entry.rdata.justv;
      highestHash = entry.rdata.justh;
    }
  }
  bool validType = newviews->size == getQsize()
                || (newviews->size == getTQsize() && allTEE);
  return validType && acc->set && acc->view == COMBview
      && acc->size == newviews->size && acc->prepv == highest
      && eqHashes(acc->hash, highestHash) && verifyAccum(acc);
}


// increments the (view,phase) pair
void COMB_increment() {
  if (COMBphase == PH1_NEWVIEW) {
    COMBphase = PH1_PREPARE;
  } else if (COMBphase == PH1_PREPARE) {
    COMBphase = PH1_PRECOMMIT;
  } else if (COMBphase == PH1_PRECOMMIT) {
    COMBphase = PH1_COMMIT;
  } else if (COMBphase == PH1_COMMIT) {
    COMBphase = PH1_NEWVIEW;
    COMBview++;
  }
}


just_t COMB_sign(hash_t h1, hash_t h2, View v2) {
  rdata_t rdata;
  rdata.proph = h1; rdata.propv = COMBview; rdata.justh = h2; rdata.justv = v2; rdata.phase = COMBphase;
  sign_t sign = signString(rdata2string(rdata));
  signs_t signs; signs.size = 1; signs.signs[0] = sign;
  just_t j; j.set = 1; j.rdata = rdata; j.signs = signs;

  COMB_increment();

  return j;
}


sgx_status_t COMB_TEEsign(just_t *just) {
  sgx_status_t status = SGX_SUCCESS;
  hash_t hash = noHash();

  *just = COMB_sign(hash,COMBpreph,COMBprepv);

  return status;
}

sgx_status_t COMB_TEEprepare(basicblock_t *block, accum_t *acc, newviews_t *newviews, just_t *res) {
  //ocall_print("TEEprepare...");
  sgx_status_t status = SGX_SUCCESS;

  //if (DEBUG0) { ocall_print((nfo() + "COMB_TEEprepare hash:" + hash->toString()).c_str()); }

  hash_t hash = COMB_hashBlock(block);
  bool safeParent = eqHashes(block->prev_hash, acc->hash)
                 && (eqHashes(block->prev_hash, COMBlockh) || acc->prepv > COMBlockv);
  if (hash.set && COMB_validateNewViews(newviews, acc) && safeParent) {
    std::map<PID, bool>::const_iterator leaderType = node_is_TEE.find(acc->sign.signer);
    if (leaderType != node_is_TEE.end() && leaderType->second) {
      COMBpreph = hash;
      COMBprepv = COMBview;
    }
    *res = COMB_sign(hash,acc->hash,acc->prepv);
  } else { res->set = false; }
  return status;
}


sgx_status_t COMB_TEEstore(just_t *just, just_t *res) {
  //ocall_print("TEEstore...");
  sgx_status_t status = SGX_SUCCESS;
  rdata_t rd = just->rdata;
  hash_t  h  = rd.proph;
  View    v  = rd.propv;
  Phase1  ph = rd.phase;
  bool validQC = (just->signs.size == getTQsize()
                  && verifyQuorum(just->signs, rdata2string(rd), getTQsize(), true))
              || (just->signs.size == getQsize()
                  && verifyQuorum(just->signs, rdata2string(rd), getQsize(), false));
  if (validQC
      && (COMBview == v)
      && (ph == PH1_PREPARE || ph == PH1_PRECOMMIT)) {
        COMBpreph=h; COMBprepv=v;
        if (ph == PH1_PRECOMMIT) {
          COMBlockh=h; COMBlockv=v;
        }
        *res = COMB_sign(h,newHash(),0);
      } else { 
        if (!(just->signs.size == getTQsize()||just->signs.size == getQsize())) { ocall_print("fail: qcsize"); }
        if (!validQC) { ocall_print("fail: verifyJust/quorum"); }
        if (!(COMBview == v)) { ocall_print("fail: view mismatch"); }
        if (!(ph == PH1_PREPARE || ph == PH1_PRECOMMIT)) { ocall_print("fail: phase invalid"); }
        res->set=false; }
  return status;
}


sgx_status_t COMB_TEEaccum(onejusts_t *js, accum_t *res) {
  sgx_status_t status = SGX_SUCCESS;

  View v = js->justs[0].rdata.propv;
  View highest = 0;
  hash_t hash = newHash();
  std::set<PID> signers;

  for (int i = 0; i < MAX_NUM_SIGNATURES && i < getQsize(); i++) {
    onejust_t just  = js->justs[i];
    rdata_t   data  = just.rdata;
    sign_t    sign  = just.sign;
    signs_t   signs; signs.size = 1; signs.signs[0] = sign;
    PID signer = sign.signer;
    if (data.phase == PH1_NEWVIEW
        && data.propv == v
        && signers.find(signer) == signers.end()
        && verifyText(signs,rdata2string(data))) {
      signers.insert(signer);
      if (data.justv >= highest) {
        highest = data.justv;
        hash = data.justh;
      }
    }
  }

  bool set = true;
  unsigned int size = signers.size();
  std::string text = std::to_string(set) + std::to_string(v) + std::to_string(highest) + hash2string(hash) + std::to_string(size);
  sign_t sign = signString(text);
  res->set = 1;
  res->view = v;
  res->prepv = highest;
  res->hash = hash;
  res->size = size;
  res->sign = sign;

  return status;
}


sgx_status_t COMB_TEEaccumSp(just_t *just, accum_t *res) {
  sgx_status_t status = SGX_SUCCESS;

  rdata_t rdata = just->rdata;
  signs_t signs = just->signs;
  hash_t  proph = rdata.proph;
  View    propv = rdata.propv;
  hash_t  justh = rdata.justh;
  View    justv = rdata.justv;
  Phase1  phase = rdata.phase;

  std::set<PID> signers;

  if (phase == PH1_NEWVIEW && verifyText(signs,rdata2string(rdata))) {
    for (int i = 0; i < MAX_NUM_SIGNATURES && i < getQsize() && i < signs.size; i++) {
      PID signer = signs.signs[i].signer;
      if (signers.find(signer) == signers.end()) { signers.insert(signer); }
    }
  }

  bool set = true;
  unsigned int size = signers.size();
  std::string text = std::to_string(set) + std::to_string(propv) + std::to_string(justv) + hash2string(justh) + std::to_string(size);
  sign_t sign = signString(text);
  res->set = 1;
  res->view = propv;
  res->prepv = justv;
  res->hash = justh;
  res->size = size;
  res->sign = sign;

  return status;
}
