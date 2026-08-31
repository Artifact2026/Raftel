#include <set>
#include "EnclaveShare.h"

// Independent trusted state for BASIC_DAMYSUS.  This is the upstream
// BASIC_CHEAP_AND_QUICK checker+accumulator state machine, deliberately kept
// separate from the hybrid COMB_* state.
hash_t DAMYSUSpreph = newHash();
View DAMYSUSprepv = 0;
View DAMYSUSview = 0;
Phase1 DAMYSUSphase = PH1_NEWVIEW;

void DAMYSUS_increment() {
  if (DAMYSUSphase == PH1_NEWVIEW) DAMYSUSphase = PH1_PREPARE;
  else if (DAMYSUSphase == PH1_PREPARE) DAMYSUSphase = PH1_PRECOMMIT;
  else if (DAMYSUSphase == PH1_PRECOMMIT) {
    DAMYSUSphase = PH1_NEWVIEW;
    ++DAMYSUSview;
  }
}

just_t DAMYSUS_sign(hash_t h1, hash_t h2, View v2) {
  rdata_t data;
  data.proph=h1; data.propv=DAMYSUSview; data.justh=h2;
  data.justv=v2; data.phase=DAMYSUSphase;
  signs_t signs; signs.size=1; signs.signs[0]=signString(rdata2string(data));
  just_t result; result.set=true; result.rdata=data; result.signs=signs;
  DAMYSUS_increment();
  return result;
}

sgx_status_t DAMYSUS_TEEsign(just_t *result) {
  *result = DAMYSUS_sign(noHash(),DAMYSUSpreph,DAMYSUSprepv);
  return SGX_SUCCESS;
}

sgx_status_t DAMYSUS_TEEprepare(hash_t *hash, accum_t *acc, just_t *result) {
  if (verifyAccum(acc) && DAMYSUSview == acc->view && acc->size == getQsize())
    *result = DAMYSUS_sign(*hash,acc->hash,acc->prepv);
  else result->set=false;
  return SGX_SUCCESS;
}

sgx_status_t DAMYSUS_TEEstore(just_t *just, just_t *result) {
  rdata_t data=just->rdata;
  if (just->signs.size == getQsize() && verifyJust(just)
      && DAMYSUSview == data.propv && data.phase == PH1_PREPARE) {
    DAMYSUSpreph=data.proph; DAMYSUSprepv=data.propv;
    *result=DAMYSUS_sign(data.proph,newHash(),0);
  } else result->set=false;
  return SGX_SUCCESS;
}

sgx_status_t DAMYSUS_TEEaccum(onejusts_t *js, accum_t *result) {
  View view=js->justs[0].rdata.propv, highest=0;
  hash_t hash=newHash();
  std::set<PID> signers;
  for (int i=0; i<MAX_NUM_SIGNATURES && i<getQsize(); ++i) {
    onejust_t just=js->justs[i];
    signs_t signs; signs.size=1; signs.signs[0]=just.sign;
    PID signer=just.sign.signer;
    if (just.rdata.phase == PH1_NEWVIEW && just.rdata.propv == view
        && signers.insert(signer).second
        && verifyText(signs,rdata2string(just.rdata))) {
      if (just.rdata.justv >= highest) {
        highest=just.rdata.justv; hash=just.rdata.justh;
      }
    }
  }
  unsigned int size=signers.size();
  std::string text=std::to_string(true)+std::to_string(view)
      +std::to_string(highest)+hash2string(hash)+std::to_string(size);
  result->set=true; result->view=view; result->prepv=highest;
  result->hash=hash; result->size=size; result->sign=signString(text);
  return SGX_SUCCESS;
}

sgx_status_t DAMYSUS_TEEaccumSp(just_t *just, accum_t *result) {
  rdata_t data=just->rdata;
  std::set<PID> signers;
  if (data.phase == PH1_NEWVIEW && verifyText(just->signs,rdata2string(data))) {
    for (int i=0; i<MAX_NUM_SIGNATURES && i<getQsize() && i<just->signs.size; ++i)
      signers.insert(just->signs.signs[i].signer);
  }
  unsigned int size=signers.size();
  std::string text=std::to_string(true)+std::to_string(data.propv)
      +std::to_string(data.justv)+hash2string(data.justh)+std::to_string(size);
  result->set=true; result->view=data.propv; result->prepv=data.justv;
  result->hash=data.justh; result->size=size; result->sign=signString(text);
  return SGX_SUCCESS;
}
