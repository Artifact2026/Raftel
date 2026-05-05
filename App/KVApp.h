#ifndef KVAPP_H
#define KVAPP_H

#include <cstdint>
#include <string>
#include <unordered_map>

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
  std::unordered_map<std::string, AppReply> dedup_cache;
  std::unordered_map<std::string, std::string> mem_fallback;
  bool warned_missing_redis_cli = false;

  std::string dedupKey(int client_id, int req_id) const;
  bool runRedisCli(const std::string &cmd, std::string &output);
  AppReply execRedis(const AppRequest &req);
  AppReply execFallback(const AppRequest &req);

 public:
  KVAppExecutor(int rid, int port);
  AppReply execute(const AppRequest &req);
};

#endif
