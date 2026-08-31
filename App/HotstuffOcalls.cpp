#include "config.h"

#if defined(BASIC_HOTSTUFF)

#include "Enclave_u.h"

#include <cstdio>

// BASIC_HOTSTUFF follows the upstream BASIC_BASELINE software-trusted path and
// never enters the enclave.  The generated untrusted bridge is still linked by
// the common build, so provide its OCall symbols without introducing SGX state.
void ocall_print(const char *str) { std::printf("%s\n", str); }
void ocall_test(KEY *) {}
void ocall_setCtime() {}
void ocall_recCStime() {}
void ocall_recCVtime() {}

#endif
