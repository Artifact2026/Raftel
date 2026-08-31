// Achilles' trusted chained Checker+Accumulator implementation is identical
// to the upstream EnclaveChComb state machine.  Rename every state/function
// symbol while compiling that implementation so this protocol owns an
// independent trusted state and independent ECALL entry points.
#define CHCOMBpreph ACHILLES_CH_preph
#define CHCOMBprepv ACHILLES_CH_prepv
#define CHCOMBview ACHILLES_CH_view
#define CHCOMBphase ACHILLES_CH_phase
#define GENblock ACHILLES_CH_genesis
#define CHCOMBincrement ACHILLES_CH_increment
#define CHCOMBsign ACHILLES_CH_sign
#define CH_COMB_TEEsign ACHILLES_CH_TEEsign
#define CH_COMB_TEEverify ACHILLES_CH_TEEverify
#define CH_COMB_TEEprepare ACHILLES_CH_TEEprepare
#define CH_COMB_TEEaccum ACHILLES_CH_TEEaccum
#define CH_COMB_TEEaccumSp ACHILLES_CH_TEEaccumSp

#include "EnclaveChComb.cpp"
