#include "KVApp.h"

#include <cstdio>
#include <cstring>

namespace {
constexpr size_t kHeaderBytes = 14;  // op(1) + reserved(1) + klen(2) + vlen(2) + cid(4) + rid(4)

static void putU16(std::array<unsigned char, PAYLOAD_SIZE> &buf, size_t off, uint16_t v) {
  buf[off] = static_cast<unsigned char>(v & 0xff);
  buf[off + 1] = static_cast<unsigned char>((v >> 8) & 0xff);
}

static uint16_t getU16(const std::array<unsigned char, PAYLOAD_SIZE> &buf, size_t off) {
  return static_cast<uint16_t>(buf[off]) |
         (static_cast<uint16_t>(buf[off + 1]) << 8);
}

static void putI32(std::array<unsigned char, PAYLOAD_SIZE> &buf, size_t off, int v) {
  buf[off] = static_cast<unsigned char>(v & 0xff);
  buf[off + 1] = static_cast<unsigned char>((v >> 8) & 0xff);
  buf[off + 2] = static_cast<unsigned char>((v >> 16) & 0xff);
  buf[off + 3] = static_cast<unsigned char>((v >> 24) & 0xff);
}

static int getI32(const std::array<unsigned char, PAYLOAD_SIZE> &buf, size_t off) {
  return static_cast<int>(buf[off]) |
         (static_cast<int>(buf[off + 1]) << 8) |
         (static_cast<int>(buf[off + 2]) << 16) |
         (static_cast<int>(buf[off + 3]) << 24);
}

static std::string shellQuote(const std::string &s) {
  std::string out = "'";
  for (char c : s) {
    if (c == '\'') { out += "'\\''"; }
    else { out += c; }
  }
  out += "'";
  return out;
}
}  // namespace

bool KVAppCodec::encode(const AppRequest &req, std::array<unsigned char, PAYLOAD_SIZE> &out) {
  for (size_t i = 0; i < PAYLOAD_SIZE; ++i) { out[i] = 0; }
  if (PAYLOAD_SIZE < kHeaderBytes) { return false; }

  size_t maxData = PAYLOAD_SIZE - kHeaderBytes;
  if (req.key.size() + req.value.size() > maxData) { return false; }

  out[0] = static_cast<unsigned char>(req.op);
  out[1] = 0;
  putU16(out, 2, static_cast<uint16_t>(req.key.size()));
  putU16(out, 4, static_cast<uint16_t>(req.value.size()));
  putI32(out, 6, req.client_id);
  putI32(out, 10, req.req_id);

  size_t off = kHeaderBytes;
  for (char c : req.key) { out[off++] = static_cast<unsigned char>(c); }
  for (char c : req.value) { out[off++] = static_cast<unsigned char>(c); }
  return true;
}

bool KVAppCodec::decode(const Transaction &tx, AppRequest &out) {
  if (PAYLOAD_SIZE < kHeaderBytes) { return false; }
  const auto &buf = tx.getPayload();

  out.op = static_cast<OpType>(buf[0]);
  uint16_t klen = getU16(buf, 2);
  uint16_t vlen = getU16(buf, 4);
  out.client_id = getI32(buf, 6);
  out.req_id = getI32(buf, 10);

  if (static_cast<size_t>(klen) + static_cast<size_t>(vlen) > (PAYLOAD_SIZE - kHeaderBytes)) {
    return false;
  }

  size_t off = kHeaderBytes;
  out.key.assign(reinterpret_cast<const char *>(&buf[off]), klen);
  off += klen;
  out.value.assign(reinterpret_cast<const char *>(&buf[off]), vlen);
  return true;
}

KVAppExecutor::KVAppExecutor(int rid, int port) : replica_id(rid), redis_port(port) {}

std::string KVAppExecutor::dedupKey(int client_id, int req_id) const {
  return std::to_string(client_id) + ":" + std::to_string(req_id);
}

bool KVAppExecutor::runRedisCli(const std::string &cmd, std::string &output) {
  std::string full = "redis-cli -p " + std::to_string(this->redis_port) + " --raw " + cmd + " 2>/dev/null";
  FILE *pipe = popen(full.c_str(), "r");
  if (pipe == nullptr) { return false; }
  char buffer[1024];
  output.clear();
  while (fgets(buffer, sizeof(buffer), pipe) != nullptr) { output += buffer; }
  int rc = pclose(pipe);
  if (rc != 0) {
    if (!this->warned_missing_redis_cli) { this->warned_missing_redis_cli = true; }
    return false;
  }
  while (!output.empty() && (output.back() == '\n' || output.back() == '\r')) { output.pop_back(); }
  return true;
}

AppReply KVAppExecutor::execRedis(const AppRequest &req) {
  AppReply rep;
  std::string out;
  if (req.op == OpType::OP_SET) {
    if (!runRedisCli("SET " + shellQuote(req.key) + " " + shellQuote(req.value), out)) {
      return execFallback(req);
    }
    rep.status = ReplyStatus::REPLY_OK;
    return rep;
  }
  if (req.op == OpType::OP_GET) {
    if (!runRedisCli("GET " + shellQuote(req.key), out)) {
      return execFallback(req);
    }
    if (out.empty()) {
      rep.status = ReplyStatus::REPLY_NOT_FOUND;
    } else {
      rep.status = ReplyStatus::REPLY_OK;
      rep.value = out;
    }
    return rep;
  }
  if (req.op == OpType::OP_DEL) {
    if (!runRedisCli("DEL " + shellQuote(req.key), out)) {
      return execFallback(req);
    }
    rep.status = ReplyStatus::REPLY_OK;
    rep.del_count = (out == "1") ? 1 : 0;
    return rep;
  }
  rep.status = ReplyStatus::REPLY_ERROR;
  return rep;
}

AppReply KVAppExecutor::execFallback(const AppRequest &req) {
  AppReply rep;
  if (req.op == OpType::OP_SET) {
    this->mem_fallback[req.key] = req.value;
    rep.status = ReplyStatus::REPLY_OK;
    return rep;
  }
  if (req.op == OpType::OP_GET) {
    auto it = this->mem_fallback.find(req.key);
    if (it == this->mem_fallback.end()) {
      rep.status = ReplyStatus::REPLY_NOT_FOUND;
    } else {
      rep.status = ReplyStatus::REPLY_OK;
      rep.value = it->second;
    }
    return rep;
  }
  if (req.op == OpType::OP_DEL) {
    auto it = this->mem_fallback.find(req.key);
    if (it == this->mem_fallback.end()) {
      rep.status = ReplyStatus::REPLY_OK;
      rep.del_count = 0;
    } else {
      this->mem_fallback.erase(it);
      rep.status = ReplyStatus::REPLY_OK;
      rep.del_count = 1;
    }
    return rep;
  }
  rep.status = ReplyStatus::REPLY_ERROR;
  return rep;
}

AppReply KVAppExecutor::execute(const AppRequest &req) {
  std::string key = dedupKey(req.client_id, req.req_id);
  auto it = dedup_cache.find(key);
  if (it != dedup_cache.end()) { return it->second; }

  AppReply rep = execRedis(req);
  dedup_cache[key] = rep;
  return rep;
}
