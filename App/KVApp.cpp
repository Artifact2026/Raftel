#include "KVApp.h"

#include <cstring>
#include <stdexcept>

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

KVAppExecutor::KVAppExecutor(int rid, int port, bool useRedis)
    : replica_id(rid), redis_port(port), use_redis(useRedis) {
  // Redis is part of the measured application path.  Starting a replica
  // without it would silently turn an end-to-end Redis benchmark into an
  // in-process unordered_map benchmark, so fail the replica immediately.
  if (use_redis && !ensureRedisConnected()) {
#if !KVAPP_HAS_HIREDIS
    throw std::runtime_error("Redis is required, but this binary was built without hiredis support");
#else
    throw std::runtime_error(
        "Redis is required, but replica " + std::to_string(replica_id) +
        " could not connect to 127.0.0.1:" + std::to_string(redis_port));
#endif
  }
}

KVAppExecutor::~KVAppExecutor() {
#if KVAPP_HAS_HIREDIS
  if (redis_ctx != nullptr) {
    redisFree(redis_ctx);
    redis_ctx = nullptr;
  }
#endif
}

std::string KVAppExecutor::dedupKey(int client_id, int req_id) const {
  return std::to_string(client_id) + ":" + std::to_string(req_id);
}

bool KVAppExecutor::ensureRedisConnected() {
#if !KVAPP_HAS_HIREDIS
  return false;
#else
  if (redis_ctx != nullptr && redis_ctx->err == 0) {
    return true;
  }
  if (redis_ctx != nullptr) {
    redisFree(redis_ctx);
    redis_ctx = nullptr;
  }
  const struct timeval timeout = {1, 0};
  redis_ctx = redisConnectWithTimeout("127.0.0.1", this->redis_port, timeout);
  if (redis_ctx == nullptr || redis_ctx->err != 0) {
    if (redis_ctx != nullptr) {
      redisFree(redis_ctx);
      redis_ctx = nullptr;
    }
    return false;
  }
  return true;
#endif
}

AppReply KVAppExecutor::execRedis(const AppRequest &req) {
#if !KVAPP_HAS_HIREDIS
  throw std::runtime_error("Redis operation attempted without hiredis support");
#else
  AppReply rep;
  if (!ensureRedisConnected()) {
    throw std::runtime_error(
        "lost Redis connection on replica " + std::to_string(replica_id) +
        " (127.0.0.1:" + std::to_string(redis_port) + ")");
  }

  if (req.op == OpType::OP_SET) {
    redisReply *reply = static_cast<redisReply *>(
        redisCommand(redis_ctx, "SET %b %b", req.key.data(), req.key.size(), req.value.data(), req.value.size()));
    if (reply == nullptr) {
      if (redis_ctx != nullptr) { redisFree(redis_ctx); redis_ctx = nullptr; }
      throw std::runtime_error("Redis SET failed on replica " + std::to_string(replica_id));
    }
    rep.status = (reply->type == REDIS_REPLY_STATUS || reply->type == REDIS_REPLY_STRING)
                     ? ReplyStatus::REPLY_OK
                     : ReplyStatus::REPLY_ERROR;
    freeReplyObject(reply);
    return rep;
  }
  if (req.op == OpType::OP_GET) {
    redisReply *reply = static_cast<redisReply *>(
        redisCommand(redis_ctx, "GET %b", req.key.data(), req.key.size()));
    if (reply == nullptr) {
      if (redis_ctx != nullptr) { redisFree(redis_ctx); redis_ctx = nullptr; }
      throw std::runtime_error("Redis GET failed on replica " + std::to_string(replica_id));
    }
    if (reply->type == REDIS_REPLY_NIL) {
      rep.status = ReplyStatus::REPLY_NOT_FOUND;
    } else if (reply->type == REDIS_REPLY_STRING || reply->type == REDIS_REPLY_STATUS) {
      rep.status = ReplyStatus::REPLY_OK;
      if (reply->str != nullptr && reply->len > 0) {
        rep.value.assign(reply->str, static_cast<size_t>(reply->len));
      }
    } else {
      rep.status = ReplyStatus::REPLY_ERROR;
    }
    freeReplyObject(reply);
    return rep;
  }
  if (req.op == OpType::OP_DEL) {
    redisReply *reply = static_cast<redisReply *>(
        redisCommand(redis_ctx, "DEL %b", req.key.data(), req.key.size()));
    if (reply == nullptr) {
      if (redis_ctx != nullptr) { redisFree(redis_ctx); redis_ctx = nullptr; }
      throw std::runtime_error("Redis DEL failed on replica " + std::to_string(replica_id));
    }
    if (reply->type == REDIS_REPLY_INTEGER) {
      rep.status = ReplyStatus::REPLY_OK;
      rep.del_count = static_cast<int>(reply->integer);
    } else {
      rep.status = ReplyStatus::REPLY_ERROR;
      rep.del_count = 0;
    }
    freeReplyObject(reply);
    return rep;
  }
  rep.status = ReplyStatus::REPLY_ERROR;
  return rep;
#endif
}

AppReply KVAppExecutor::execute(const AppRequest &req) {
  std::string key = dedupKey(req.client_id, req.req_id);
  auto it = dedup_cache.find(key);
  if (it != dedup_cache.end()) { return it->second; }

  AppReply rep = use_redis ? execRedis(req) : execMemory(req);
  dedup_cache[key] = rep;
  return rep;
}

AppReply KVAppExecutor::execMemory(const AppRequest &req) {
  AppReply rep;
  if (req.op == OpType::OP_SET) {
    mem_store[req.key] = req.value;
    rep.status = ReplyStatus::REPLY_OK;
  } else if (req.op == OpType::OP_GET) {
    auto it = mem_store.find(req.key);
    if (it == mem_store.end()) {
      rep.status = ReplyStatus::REPLY_NOT_FOUND;
    } else {
      rep.status = ReplyStatus::REPLY_OK;
      rep.value = it->second;
    }
  } else if (req.op == OpType::OP_DEL) {
    rep.status = ReplyStatus::REPLY_OK;
    rep.del_count = static_cast<int>(mem_store.erase(req.key));
  } else {
    rep.status = ReplyStatus::REPLY_ERROR;
  }
  return rep;
}
