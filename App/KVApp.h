#ifndef KVAPP_H
#define KVAPP_H

#include <cstdint>
#include <string>
#include <unordered_map>

#if __has_include(<hiredis/hiredis.h>)
#define KVAPP_HAS_HIREDIS 1
#include <hiredis/hiredis.h>
#elif __has_include(<hiredis.h>)
#define KVAPP_HAS_HIREDIS 1
#include <hiredis.h>
#else
#define KVAPP_HAS_HIREDIS 0
#endif

#include "Transaction.h"

enum class OpType : uint8_t {
  OP_INVALID = 0,
  OP_SET = 1,
  OP_GET = 2,
  OP_DEL = 3,
};

struct AppRequest {
  OpType op = OpType::OP_INVALID;
  std::string key;
  std::string value;
  int client_id = 0;
  int req_id = 0;
};

enum class ReplyStatus : uint8_t {
  REPLY_OK = 0,
  REPLY_NOT_FOUND = 1,
  REPLY_ERROR = 2,
};

struct AppReply {
  ReplyStatus status = ReplyStatus::REPLY_ERROR;
  std::string value;
  int del_count = 0;
};

class KVAppCodec {
 public:
  static bool encode(const AppRequest &req, std::array<unsigned char, PAYLOAD_SIZE> &out);
  static bool decode(const Transaction &tx, AppRequest &out);
};

class KVAppExecutor {
 private:
  int replica_id;
  int redis_port;
  bool use_redis;
#if KVAPP_HAS_HIREDIS
  redisContext *redis_ctx = nullptr;
#else
  void *redis_ctx = nullptr;
#endif
  std::unordered_map<std::string, AppReply> dedup_cache;
  std::unordered_map<std::string, std::string> mem_store;

  std::string dedupKey(int client_id, int req_id) const;
  bool ensureRedisConnected();
  AppReply execRedis(const AppRequest &req);
  AppReply execMemory(const AppRequest &req);

 public:
  KVAppExecutor(int rid, int port, bool useRedis);
  ~KVAppExecutor();
  AppReply execute(const AppRequest &req);
};

#endif
