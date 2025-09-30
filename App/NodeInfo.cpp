#include <cstring>
#include <iostream>
#include <assert.h>
#include <unistd.h>

#include "NodeInfo.h"


NodeInfo::NodeInfo() {
  this->pid   = 0;
  this->pub   = NULL;
  this->host  = "";
  this->rport = 0;
  this->cport = 0;
  this->isTEE = false;
}


NodeInfo::NodeInfo(PID pid, HOST host, PORT rport, PORT cport, bool isTEE) {
  this->pid   = pid;
  this->pub   = NULL;
  this->host  = host;
  this->rport = rport;
  this->cport = cport;
  this->isTEE = isTEE;
}


NodeInfo::NodeInfo(PID pid, KEY pub, HOST host, PORT rport, PORT cport, bool isTEE) {
  this->pid   = pid;
  this->pub   = pub;
  this->host  = host;
  this->rport = rport;
  this->cport = cport;
  this->isTEE = isTEE;
}


PID  NodeInfo::getId()    { return this->pid;   }
KEY  NodeInfo::getPub()   { return this->pub;   }
HOST NodeInfo::getHost()  { return this->host;  }
PORT NodeInfo::getRPort() { return this->rport; }
PORT NodeInfo::getCPort() { return this->cport; }
bool NodeInfo::getIsTEE() { return this->isTEE; }
void NodeInfo::setIsTEE(bool isTEE) { this->isTEE = isTEE; }


void NodeInfo::setPub(KEY pub) {
  this->pub = pub;
}


std::string NodeInfo::toString() {
  return ("NFO[id=" + std::to_string(this->pid)
          + ";pub=_"
          + ";host=" + this->host
          + ";rport=" + std::to_string(this->rport)
          + ";cport=" + std::to_string(this->cport)
          + ";isTEE=" + (this->isTEE ? "true" : "false")
          + "]");
}


// TODO: finish
bool NodeInfo::operator<(const NodeInfo& hp) const {
  if (pid < hp.pid) { return true; }
  return false;
}
