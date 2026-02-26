#!/usr/bin/env python3
import argparse
import hashlib
import json
import math
import random
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set

MESSAGE_TYPES = {
    "REGISTER_REQ",
    "REGISTER_VOTE",
    "REGISTER_COMMIT",
    "PEER_LIST_REQ",
    "PEER_LIST_RESP",
    "HELLO",
    "HELLO_ACK",
    "GOSSIP_MSG",
    "SUSPECT_REQ",
    "SUSPECT_ACK",
    "DEAD_REPORT",
    "SEED_DEAD_VOTE",
    "SEED_DEAD_COMMIT",
    "ERROR",
}

GOSSIP_INTERVAL_SECONDS = 5
MAX_GOSSIP_MESSAGES = 10
QUORUM_FORMULA = "floor(n/2)+1"
PING_INTERVAL_SECONDS = 4
PING_FAILURE_THRESHOLD = 2
SUSPECT_COOLDOWN_SECONDS = 20


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


def gossip_text(self_ip: str, msg_number: int, timestamp: Optional[int] = None) -> str:
    ts = int(timestamp if timestamp is not None else time.time())
    return f"{ts}:{self_ip}:{msg_number}"


def dead_report_text(dead_ip: str, dead_port: int, self_ip: str, timestamp: Optional[int] = None) -> str:
    ts = int(timestamp if timestamp is not None else time.time())
    return f"Dead Node:{dead_ip}:{dead_port}:{ts}:{self_ip}"


def msg_hash(message: str) -> str:
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


class PeerNode:
    def __init__(self, ip: str, port: int, config_path: str, output_file: str) -> None:
        self.self_ep = Endpoint(ip=ip, port=port)
        self.seeds = load_seed_config(config_path)
        self.quorum = required_quorum(len(self.seeds))
        self.connected_seeds: List[Endpoint] = []
        self.neighbors: Set[str] = set()
        self.topology: Dict[str, Set[str]] = {}
        self.observed_degrees: Dict[str, int] = {}
        self.message_list: Set[str] = set()
        self.message_metadata: Dict[str, Dict[str, str]] = {}
        self.output_file = output_file
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.failed_ping_counts: Dict[str, int] = {}
        self.last_dead_report_at: Dict[str, float] = {}

    def log(self, message: str) -> None:
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [PEER {self.self_ep.key()}] {message}"
        print(line)
        Path(self.output_file).parent.mkdir(parents=True, exist_ok=True)
        with open(self.output_file, "a", encoding="utf-8") as fp:
            fp.write(line + "\n")

    def pick_registration_seeds(self) -> List[Endpoint]:
        if len(self.seeds) < self.quorum:
            raise RuntimeError("Insufficient seeds in config for quorum")
        return random.sample(self.seeds, self.quorum)

    def _recv_line(self, conn: socket.socket) -> str:
        data = b""
        while b"\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
        return data.decode("utf-8").strip()

    def _send_request(self, endpoint: Endpoint, msg_type: str, payload: Dict, timeout: float = 2.0) -> Dict:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect((endpoint.ip, endpoint.port))
            client.sendall(encode_message(msg_type, payload))
            raw = self._recv_line(client)
        return decode_message(raw)

    def _register_with_seed(self, seed: Endpoint) -> bool:
        payload = {"peer_ip": self.self_ep.ip, "peer_port": self.self_ep.port}
        try:
            response = self._send_request(seed, "REGISTER_REQ", payload)
            success = bool(response.get("payload", {}).get("success", False))
            self.log(f"Seed {seed.key()} register response: success={success}")
            return success
        except Exception as exc:
            self.log(f"Seed {seed.key()} registration failed: {exc}")
            return False

    def register_with_quorum(self) -> List[Endpoint]:
        selected = self.pick_registration_seeds()
        success_seeds: List[Endpoint] = []
        for seed in selected:
            if self._register_with_seed(seed):
                success_seeds.append(seed)

        if len(success_seeds) < self.quorum:
            raise RuntimeError(
                f"Registration quorum not met. success={len(success_seeds)}, required={self.quorum}"
            )

        self.log(f"Registration quorum achieved with {len(success_seeds)} seeds")
        return success_seeds

    def fetch_union_peer_list(self, seeds: List[Endpoint]) -> Set[str]:
        union_peers: Set[str] = set()
        for seed in seeds:
            try:
                response = self._send_request(seed, "PEER_LIST_REQ", {"requester": self.self_ep.key()})
                peers = response.get("payload", {}).get("peers", [])
                for peer in peers:
                    if peer != self.self_ep.key():
                        union_peers.add(str(peer))
                self.log(f"Received {len(peers)} peers from seed {seed.key()}")
            except Exception as exc:
                self.log(f"Peer list fetch failed from seed {seed.key()}: {exc}")

        self.log(f"Union peer list size={len(union_peers)}")
        return union_peers

    def _parse_peer_key(self, peer_key: str) -> Endpoint:
        ip, port_text = peer_key.rsplit(":", 1)
        return Endpoint(ip=ip, port=int(port_text))

    def _sender_ip(self, sender: str) -> str:
        if ":" in sender:
            return sender.rsplit(":", 1)[0]
        return sender

    def _is_host_alive(self, ip: str, timeout_seconds: float = 2.0) -> bool:
        try:
            result = subprocess.run(
                ["ping", "-c", "1", ip],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout_seconds,
                check=False,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _is_peer_port_open(self, ip: str, port: int, timeout_seconds: float = 1.2) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
                client.settimeout(timeout_seconds)
                client.connect((ip, port))
            return True
        except Exception:
            return False

    def _is_peer_responsive(self, ip: str, port: int) -> bool:
        ping_alive = self._is_host_alive(ip)
        port_open = self._is_peer_port_open(ip, port)
        return ping_alive and port_open

    def _target_degree(self, candidate_count: int) -> int:
        if candidate_count <= 0:
            return 0
        sample = int(random.paretovariate(2.3))
        sample = max(1, sample)
        return min(max(2, sample), candidate_count)

    def select_neighbors_power_law(self, candidates: Set[str]) -> List[str]:
        if not candidates:
            return []

        candidate_list = list(candidates)
        min_neighbors = min(2, len(candidate_list))
        target = max(min_neighbors, self._target_degree(len(candidate_list)))

        selected: List[str] = []
        available = candidate_list.copy()
        while available and len(selected) < target:
            weights = [max(1, self.observed_degrees.get(peer, 1)) for peer in available]
            chosen = random.choices(available, weights=weights, k=1)[0]
            selected.append(chosen)
            available.remove(chosen)

        self.log(f"Power-law neighbor selection picked {len(selected)} neighbors")
        return selected

    def _handle_hello(self, payload: Dict) -> Dict:
        peer_ip = payload["peer_ip"]
        peer_port = int(payload["peer_port"])
        peer_key = f"{peer_ip}:{peer_port}"
        peer_degree = int(payload.get("degree", 1))

        with self.lock:
            self.neighbors.add(peer_key)
            self.observed_degrees[peer_key] = max(1, peer_degree)
            self.topology.setdefault(self.self_ep.key(), set()).add(peer_key)
            self.topology.setdefault(peer_key, set()).add(self.self_ep.key())

        self.log(f"HELLO accepted from {peer_key}; neighbor linked")
        return {
            "type": "HELLO_ACK",
            "payload": {
                "accepted": True,
                "peer_ip": self.self_ep.ip,
                "peer_port": self.self_ep.port,
                "degree": len(self.neighbors),
            },
        }

    def _forward_gossip(self, message: str, origin_ip: str, msg_no: int, exclude_sender: Optional[str]) -> None:
        with self.lock:
            targets = [neighbor for neighbor in self.neighbors if neighbor != exclude_sender]

        payload = {
            "message": message,
            "origin_ip": origin_ip,
            "msg_no": msg_no,
            "sender": self.self_ep.key(),
        }
        for neighbor_key in targets:
            endpoint = self._parse_peer_key(neighbor_key)
            try:
                self._send_request(endpoint, "GOSSIP_MSG", payload, timeout=1.5)
            except Exception as exc:
                self.log(f"Gossip forward failed to {neighbor_key}: {exc}")

    def _handle_gossip(self, payload: Dict) -> Dict:
        message = str(payload.get("message", "")).strip()
        if not message:
            return {"type": "GOSSIP_MSG", "payload": {"accepted": False, "reason": "empty_message"}}

        origin_ip = str(payload.get("origin_ip", "unknown"))
        sender = str(payload.get("sender", "unknown"))
        msg_no = int(payload.get("msg_no", -1))
        digest = msg_hash(message)

        with self.lock:
            if digest in self.message_list:
                self.log(f"Duplicate gossip ignored: hash={digest[:10]} sender={sender}")
                return {"type": "GOSSIP_MSG", "payload": {"accepted": True, "duplicate": True}}

            self.message_list.add(digest)
            self.message_metadata[digest] = {
                "message": message,
                "origin_ip": origin_ip,
                "sender": sender,
                "first_seen_ts": str(int(time.time())),
            }

        sender_ip = self._sender_ip(sender)
        self.log(f"First-time gossip received: {message} | sender_ip={sender_ip}")
        self._forward_gossip(message=message, origin_ip=origin_ip, msg_no=msg_no, exclude_sender=sender)
        return {"type": "GOSSIP_MSG", "payload": {"accepted": True, "duplicate": False}}

    def _handle_suspect_req(self, payload: Dict) -> Dict:
        dead_ip = str(payload.get("dead_ip", ""))
        dead_port = int(payload.get("dead_port", 0))
        suspect_key = f"{dead_ip}:{dead_port}"
        alive = self._is_peer_responsive(dead_ip, dead_port)

        self.log(
            f"Suspicion probe received for {suspect_key} from {payload.get('requester', 'unknown')} -> alive={alive}"
        )
        return {
            "type": "SUSPECT_ACK",
            "payload": {
                "suspect": suspect_key,
                "alive": alive,
                "confirmer": self.self_ep.key(),
            },
        }

    def _report_dead_to_seeds(self, dead_key: str) -> None:
        endpoint = self._parse_peer_key(dead_key)
        payload = {
            "dead_ip": endpoint.ip,
            "dead_port": endpoint.port,
            "self_ip": self.self_ep.ip,
            "timestamp": int(time.time()),
            "reporter": self.self_ep.key(),
        }

        success_count = 0
        for seed in self.connected_seeds:
            try:
                response = self._send_request(seed, "DEAD_REPORT", payload, timeout=2.0)
                accepted = bool(response.get("payload", {}).get("success", False))
                if accepted:
                    success_count += 1
            except Exception as exc:
                self.log(f"Dead report send failed to seed {seed.key()}: {exc}")

        self.log(
            f"Confirmed dead-node report sent for {dead_key}. seed_accepts={success_count}/{len(self.connected_seeds)}"
        )

    def _peer_level_suspicion_consensus(self, dead_key: str) -> bool:
        dead_endpoint = self._parse_peer_key(dead_key)

        with self.lock:
            peers_to_ask = [neighbor for neighbor in self.neighbors if neighbor != dead_key]

        if not peers_to_ask:
            self.log(f"Suspicion for {dead_key} ignored: no other peers available for confirmation")
            return False

        self_vote_dead = True
        external_votes = 0
        external_dead_votes = 0

        for neighbor_key in peers_to_ask:
            endpoint = self._parse_peer_key(neighbor_key)
            payload = {
                "dead_ip": dead_endpoint.ip,
                "dead_port": dead_endpoint.port,
                "requester": self.self_ep.key(),
            }
            try:
                response = self._send_request(endpoint, "SUSPECT_REQ", payload, timeout=2.0)
                if response.get("type") != "SUSPECT_ACK":
                    continue
                alive = bool(response.get("payload", {}).get("alive", True))
                external_votes += 1
                if not alive:
                    external_dead_votes += 1
            except Exception as exc:
                self.log(f"Suspicion probe failed to {neighbor_key}: {exc}")

        total_votes = 1 + external_votes
        dead_votes = (1 if self_vote_dead else 0) + external_dead_votes
        needed_majority = math.floor(total_votes / 2) + 1
        has_multiple_confirmers = external_dead_votes >= 1
        consensus = has_multiple_confirmers and dead_votes >= needed_majority

        self.log(
            f"Peer-level suspicion result for {dead_key}: dead_votes={dead_votes}/{total_votes}, "
            f"majority={needed_majority}, external_dead_votes={external_dead_votes}, consensus={consensus}"
        )
        return consensus

    def _liveness_loop(self) -> None:
        while not self.stop_event.is_set():
            with self.lock:
                snapshot_neighbors = list(self.neighbors)

            for neighbor_key in snapshot_neighbors:
                endpoint = self._parse_peer_key(neighbor_key)
                alive = self._is_peer_responsive(endpoint.ip, endpoint.port)

                with self.lock:
                    if alive:
                        self.failed_ping_counts[neighbor_key] = 0
                        continue

                    misses = self.failed_ping_counts.get(neighbor_key, 0) + 1
                    self.failed_ping_counts[neighbor_key] = misses
                    last_report = self.last_dead_report_at.get(neighbor_key, 0.0)

                if misses < PING_FAILURE_THRESHOLD:
                    continue

                now = time.time()
                if now - last_report < SUSPECT_COOLDOWN_SECONDS:
                    continue

                self.log(f"Neighbor suspicion triggered for {neighbor_key} after {misses} ping failures")
                consensus = self._peer_level_suspicion_consensus(neighbor_key)
                if consensus:
                    with self.lock:
                        self.last_dead_report_at[neighbor_key] = time.time()
                        self.neighbors.discard(neighbor_key)
                        self.topology.setdefault(self.self_ep.key(), set()).discard(neighbor_key)
                    self._report_dead_to_seeds(neighbor_key)

            time.sleep(PING_INTERVAL_SECONDS)

    def _gossip_loop(self) -> None:
        for msg_number in range(1, MAX_GOSSIP_MESSAGES + 1):
            if self.stop_event.is_set():
                return

            message = gossip_text(self.self_ep.ip, msg_number)
            digest = msg_hash(message)
            with self.lock:
                self.message_list.add(digest)
                self.message_metadata[digest] = {
                    "message": message,
                    "origin_ip": self.self_ep.ip,
                    "sender": self.self_ep.key(),
                    "first_seen_ts": str(int(time.time())),
                }

            self.log(f"Generated gossip: {message}")
            self._forward_gossip(message=message, origin_ip=self.self_ep.ip, msg_no=msg_number, exclude_sender=None)
            time.sleep(GOSSIP_INTERVAL_SECONDS)

    def _route_message(self, message: Dict) -> Dict:
        msg_type = message.get("type")
        payload = message.get("payload", {})
        if msg_type == "HELLO":
            return self._handle_hello(payload)
        if msg_type == "GOSSIP_MSG":
            return self._handle_gossip(payload)
        if msg_type == "SUSPECT_REQ":
            return self._handle_suspect_req(payload)

        return {"type": "ERROR", "payload": {"reason": f"Unsupported type: {msg_type}"}}

    def _handle_connection(self, conn: socket.socket, addr: str) -> None:
        try:
            raw = self._recv_line(conn)
            if not raw:
                return
            message = decode_message(raw)
            response = self._route_message(message)
            conn.sendall(encode_message(response["type"], response["payload"]))
        except Exception as exc:
            self.log(f"Connection handler error from {addr}: {exc}")
            try:
                conn.sendall(encode_message("ERROR", {"reason": str(exc)}))
            except Exception:
                pass
        finally:
            conn.close()

    def _listen_loop(self, server: socket.socket) -> None:
        while not self.stop_event.is_set():
            try:
                conn, addr = server.accept()
            except OSError:
                break
            thread = threading.Thread(
                target=self._handle_connection,
                args=(conn, f"{addr[0]}:{addr[1]}"),
                daemon=True,
            )
            thread.start()

    def connect_neighbors(self, selected_neighbors: List[str]) -> None:
        for peer_key in selected_neighbors:
            endpoint = self._parse_peer_key(peer_key)
            try:
                payload = {
                    "peer_ip": self.self_ep.ip,
                    "peer_port": self.self_ep.port,
                    "degree": len(self.neighbors),
                }
                response = self._send_request(endpoint, "HELLO", payload, timeout=2.0)
                accepted = bool(response.get("payload", {}).get("accepted", False))

                if accepted:
                    with self.lock:
                        self.neighbors.add(peer_key)
                        remote_degree = int(response.get("payload", {}).get("degree", 1))
                        self.observed_degrees[peer_key] = max(1, remote_degree)
                        self.topology.setdefault(self.self_ep.key(), set()).add(peer_key)
                        self.topology.setdefault(peer_key, set()).add(self.self_ep.key())
                    self.log(f"Connected neighbor: {peer_key}")
                else:
                    self.log(f"Neighbor refused HELLO: {peer_key}")
            except Exception as exc:
                self.log(f"Neighbor connect failed for {peer_key}: {exc}")

    def bootstrap_overlay(self) -> None:
        self.connected_seeds = self.register_with_quorum()
        peers_union = self.fetch_union_peer_list(self.connected_seeds)

        with self.lock:
            self.topology.setdefault(self.self_ep.key(), set())
            for peer in peers_union:
                self.topology.setdefault(peer, set())
                self.observed_degrees.setdefault(peer, 1)

        selected_neighbors = self.select_neighbors_power_law(peers_union)
        self.connect_neighbors(selected_neighbors)
        self.log(
            f"Bootstrap complete: seeds={len(self.connected_seeds)}, union_peers={len(peers_union)}, neighbors={len(self.neighbors)}"
        )

    def run(self) -> None:
        self.log(f"Starting peer with {len(self.seeds)} seeds configured. Quorum={self.quorum}")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((self.self_ep.ip, self.self_ep.port))
            server.listen()
            self.log("Listening for incoming peer connections.")

            listener = threading.Thread(target=self._listen_loop, args=(server,), daemon=True)
            listener.start()

            self.bootstrap_overlay()

            gossip_thread = threading.Thread(target=self._gossip_loop, daemon=True)
            gossip_thread.start()

            liveness_thread = threading.Thread(target=self._liveness_loop, daemon=True)
            liveness_thread.start()

            while True:
                time.sleep(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Peer node for gossip P2P assignment")
    parser.add_argument("--ip", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--config", default="config.txt")
    parser.add_argument("--output", default="outputfile.txt")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    node = PeerNode(ip=args.ip, port=args.port, config_path=args.config, output_file=args.output)
    node.run()


if __name__ == "__main__":
    main()
