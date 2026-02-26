#!/usr/bin/env python3
import argparse
import json
import math
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Set

MESSAGE_TYPES = {
    "REGISTER_REQ",
    "REGISTER_VOTE",
    "REGISTER_COMMIT",
    "PEER_LIST_REQ",
    "PEER_LIST_RESP",
    "DEAD_REPORT",
    "SEED_DEAD_VOTE",
    "SEED_DEAD_COMMIT",
    "ERROR",
}

QUORUM_FORMULA = "floor(n/2)+1"


@dataclass(frozen=True)
class Endpoint:
    ip: str
    port: int

    def key(self) -> str:
        return f"{self.ip}:{self.port}"


def load_seed_config(config_path: str) -> List[Endpoint]:
    seeds: List[Endpoint] = []
    for line in Path(config_path).read_text().splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        parts = [x.strip() for x in raw.replace(" ", "").split(",")]
        if len(parts) != 2:
            continue
        ip, port = parts[0], int(parts[1])
        seeds.append(Endpoint(ip=ip, port=port))
    return seeds


def required_quorum(total_seeds: int) -> int:
    return math.floor(total_seeds / 2) + 1


def encode_message(msg_type: str, payload: Dict) -> bytes:
    if msg_type not in MESSAGE_TYPES:
        raise ValueError(f"Unsupported message type: {msg_type}")
    body = {"type": msg_type, "payload": payload, "ts": time.time()}
    return (json.dumps(body) + "\n").encode("utf-8")


def decode_message(data: str) -> Dict:
    parsed = json.loads(data)
    if "type" not in parsed or "payload" not in parsed:
        raise ValueError("Invalid message format")
    if parsed["type"] not in MESSAGE_TYPES:
        raise ValueError("Unknown message type")
    return parsed


class SeedNode:
    def __init__(self, ip: str, port: int, config_path: str, output_file: str) -> None:
        self.self_ep = Endpoint(ip=ip, port=port)
        self.seeds = load_seed_config(config_path)
        self.other_seeds = [seed for seed in self.seeds if seed.key() != self.self_ep.key()]
        self.peer_list: Set[str] = set()
        self.pending_registrations: Dict[str, Set[str]] = {}
        self.pending_removals: Dict[str, Set[str]] = {}
        self.output_file = output_file
        self.lock = threading.Lock()
        self.quorum = required_quorum(len(self.seeds))

    def log(self, message: str) -> None:
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [SEED {self.self_ep.key()}] {message}"
        print(line)
        Path(self.output_file).parent.mkdir(parents=True, exist_ok=True)
        with open(self.output_file, "a", encoding="utf-8") as fp:
            fp.write(line + "\n")

    def _send_request(self, endpoint: Endpoint, msg_type: str, payload: Dict, timeout: float = 1.8) -> Dict:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect((endpoint.ip, endpoint.port))
            client.sendall(encode_message(msg_type, payload))
            data = b""
            while b"\n" not in data:
                chunk = client.recv(4096)
                if not chunk:
                    break
                data += chunk
        raw = data.decode("utf-8").strip()
        return decode_message(raw)

    def _broadcast_commit(self, msg_type: str, payload: Dict) -> None:
        for endpoint in self.other_seeds:
            try:
                self._send_request(endpoint, msg_type, payload)
            except Exception as exc:
                self.log(f"Commit sync failed to seed {endpoint.key()}: {exc}")

    def _extract_peer(self, payload: Dict) -> str:
        return f"{payload['peer_ip']}:{int(payload['peer_port'])}"

    def _extract_dead(self, payload: Dict) -> str:
        return f"{payload['dead_ip']}:{int(payload['dead_port'])}"

    def _count_yes_votes(self, responses: List[Dict], expected_type: str) -> int:
        yes_votes = 1
        for response in responses:
            if response.get("type") != expected_type:
                continue
            vote = bool(response.get("payload", {}).get("vote", False))
            if vote:
                yes_votes += 1
        return yes_votes

    def _collect_seed_votes(self, msg_type: str, payload: Dict) -> List[Dict]:
        responses: List[Dict] = []
        for endpoint in self.other_seeds:
            try:
                response = self._send_request(endpoint, msg_type, payload)
                responses.append(response)
            except Exception as exc:
                self.log(f"Vote RPC failed to seed {endpoint.key()}: {exc}")
        return responses

    def _handle_register_req(self, payload: Dict) -> Dict:
        peer_key = self._extract_peer(payload)
        proposal_id = f"reg:{peer_key}"

        with self.lock:
            if peer_key in self.peer_list:
                self.log(f"Registration idempotent success for {peer_key}")
                return {
                    "type": "REGISTER_COMMIT",
                    "payload": {"proposal_id": proposal_id, "success": True, "quorum": self.quorum},
                }
            self.pending_registrations.setdefault(proposal_id, set()).add(self.self_ep.key())

        self.log(f"Registration proposal received for {peer_key}; starting seed consensus")
        vote_payload = {
            "action": "REQUEST",
            "proposal_id": proposal_id,
            "peer_ip": payload["peer_ip"],
            "peer_port": int(payload["peer_port"]),
            "proposer": self.self_ep.key(),
        }
        responses = self._collect_seed_votes("REGISTER_VOTE", vote_payload)
        yes_votes = self._count_yes_votes(responses, "REGISTER_VOTE")

        committed = yes_votes >= self.quorum
        with self.lock:
            if committed:
                self.peer_list.add(peer_key)
            self.pending_registrations.setdefault(proposal_id, set()).add(self.self_ep.key())

        decision = "COMMIT" if committed else "ABORT"
        self.log(
            f"Registration consensus {decision} for {peer_key}. yes_votes={yes_votes}, quorum={self.quorum}"
        )

        commit_payload = {
            "proposal_id": proposal_id,
            "decision": decision,
            "peer_ip": payload["peer_ip"],
            "peer_port": int(payload["peer_port"]),
            "source_seed": self.self_ep.key(),
            "ack_only": True,
        }
        self._broadcast_commit("REGISTER_COMMIT", commit_payload)

        return {
            "type": "REGISTER_COMMIT",
            "payload": {
                "proposal_id": proposal_id,
                "success": committed,
                "yes_votes": yes_votes,
                "quorum": self.quorum,
            },
        }

    def _handle_register_vote(self, payload: Dict) -> Dict:
        action = payload.get("action", "REQUEST")
        if action == "RESPONSE":
            return {"type": "REGISTER_VOTE", "payload": {"vote": False, "ack_only": True}}

        peer_key = self._extract_peer(payload)
        proposal_id = payload["proposal_id"]
        vote = True

        with self.lock:
            self.pending_registrations.setdefault(proposal_id, set()).add(payload.get("proposer", "unknown"))
            if peer_key in self.peer_list:
                vote = True

        self.log(f"Vote on registration proposal {proposal_id} for {peer_key}: vote={vote}")
        return {
            "type": "REGISTER_VOTE",
            "payload": {
                "action": "RESPONSE",
                "proposal_id": proposal_id,
                "vote": vote,
                "voter": self.self_ep.key(),
            },
        }

    def _handle_register_commit(self, payload: Dict) -> Dict:
        decision = payload.get("decision", "ABORT")
        peer_key = self._extract_peer(payload)
        proposal_id = payload["proposal_id"]

        with self.lock:
            if decision == "COMMIT":
                self.peer_list.add(peer_key)
            self.pending_registrations.setdefault(proposal_id, set()).add(payload.get("source_seed", "unknown"))

        self.log(f"Registration commit sync: proposal={proposal_id}, peer={peer_key}, decision={decision}")
        return {"type": "REGISTER_COMMIT", "payload": {"ack": True, "ack_only": True}}

    def _handle_peer_list_req(self) -> Dict:
        with self.lock:
            peers = sorted(self.peer_list)
        self.log(f"Serving PEER_LIST_RESP with {len(peers)} peers")
        return {"type": "PEER_LIST_RESP", "payload": {"peers": peers, "seed": self.self_ep.key()}}

    def _handle_dead_report(self, payload: Dict) -> Dict:
        dead_key = self._extract_dead(payload)
        reporter = payload.get("reporter", payload.get("self_ip", "unknown"))
        proposal_id = f"dead:{dead_key}"

        with self.lock:
            self.pending_removals.setdefault(proposal_id, set()).add(str(reporter))

        self.log(f"Dead report received for {dead_key} from {reporter}; starting seed consensus")

        vote_payload = {
            "action": "REQUEST",
            "proposal_id": proposal_id,
            "dead_ip": payload["dead_ip"],
            "dead_port": int(payload["dead_port"]),
            "proposer": self.self_ep.key(),
        }
        responses = self._collect_seed_votes("SEED_DEAD_VOTE", vote_payload)
        yes_votes = self._count_yes_votes(responses, "SEED_DEAD_VOTE")
        committed = yes_votes >= self.quorum

        with self.lock:
            if committed and dead_key in self.peer_list:
                self.peer_list.remove(dead_key)

        decision = "COMMIT" if committed else "ABORT"
        self.log(f"Dead-node consensus {decision} for {dead_key}. yes_votes={yes_votes}, quorum={self.quorum}")

        commit_payload = {
            "proposal_id": proposal_id,
            "decision": decision,
            "dead_ip": payload["dead_ip"],
            "dead_port": int(payload["dead_port"]),
            "source_seed": self.self_ep.key(),
            "ack_only": True,
        }
        self._broadcast_commit("SEED_DEAD_COMMIT", commit_payload)

        return {
            "type": "SEED_DEAD_COMMIT",
            "payload": {
                "proposal_id": proposal_id,
                "success": committed,
                "yes_votes": yes_votes,
                "quorum": self.quorum,
            },
        }

    def _handle_seed_dead_vote(self, payload: Dict) -> Dict:
        action = payload.get("action", "REQUEST")
        if action == "RESPONSE":
            return {"type": "SEED_DEAD_VOTE", "payload": {"vote": False, "ack_only": True}}

        dead_key = self._extract_dead(payload)
        proposal_id = payload["proposal_id"]
        with self.lock:
            self.pending_removals.setdefault(proposal_id, set()).add(payload.get("proposer", "unknown"))
            vote = dead_key in self.peer_list

        self.log(f"Vote on dead-node proposal {proposal_id} for {dead_key}: vote={vote}")
        return {
            "type": "SEED_DEAD_VOTE",
            "payload": {
                "action": "RESPONSE",
                "proposal_id": proposal_id,
                "vote": vote,
                "voter": self.self_ep.key(),
            },
        }

    def _handle_seed_dead_commit(self, payload: Dict) -> Dict:
        decision = payload.get("decision", "ABORT")
        dead_key = self._extract_dead(payload)
        proposal_id = payload["proposal_id"]

        with self.lock:
            if decision == "COMMIT" and dead_key in self.peer_list:
                self.peer_list.remove(dead_key)
            self.pending_removals.setdefault(proposal_id, set()).add(payload.get("source_seed", "unknown"))

        self.log(f"Dead-node commit sync: proposal={proposal_id}, node={dead_key}, decision={decision}")
        return {"type": "SEED_DEAD_COMMIT", "payload": {"ack": True, "ack_only": True}}

    def _route_message(self, message: Dict) -> Dict:
        msg_type = message["type"]
        payload = message.get("payload", {})

        if msg_type == "REGISTER_REQ":
            return self._handle_register_req(payload)
        if msg_type == "REGISTER_VOTE":
            return self._handle_register_vote(payload)
        if msg_type == "REGISTER_COMMIT":
            return self._handle_register_commit(payload)
        if msg_type == "PEER_LIST_REQ":
            return self._handle_peer_list_req()
        if msg_type == "DEAD_REPORT":
            return self._handle_dead_report(payload)
        if msg_type == "SEED_DEAD_VOTE":
            return self._handle_seed_dead_vote(payload)
        if msg_type == "SEED_DEAD_COMMIT":
            return self._handle_seed_dead_commit(payload)

        return {"type": "ERROR", "payload": {"reason": f"Unsupported type: {msg_type}"}}

    def _handle_connection(self, conn: socket.socket, addr: str) -> None:
        try:
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk

            if not data:
                return

            message = decode_message(data.decode("utf-8").strip())
            response = self._route_message(message)
            conn.sendall(encode_message(response["type"], response["payload"]))
        except Exception as exc:
            self.log(f"Connection handler error from {addr}: {exc}")
            try:
                conn.sendall(encode_message("REGISTER_COMMIT", {"success": False, "error": str(exc)}))
            except Exception:
                pass
        finally:
            conn.close()

    def run(self) -> None:
        self.log(f"Booting with {len(self.seeds)} seeds. Quorum={self.quorum}")
        self.log("Task 2 active: seed consensus for registration/removal enabled.")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((self.self_ep.ip, self.self_ep.port))
            server.listen()
            self.log("Listening for incoming connections.")
            while True:
                conn, addr = server.accept()
                thread = threading.Thread(
                    target=self._handle_connection,
                    args=(conn, f"{addr[0]}:{addr[1]}"),
                    daemon=True,
                )
                thread.start()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed node for gossip P2P assignment")
    parser.add_argument("--ip", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--config", default="config.txt")
    parser.add_argument("--output", default="outputfile.txt")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    node = SeedNode(ip=args.ip, port=args.port, config_path=args.config, output_file=args.output)
    node.run()


if __name__ == "__main__":
    main()
