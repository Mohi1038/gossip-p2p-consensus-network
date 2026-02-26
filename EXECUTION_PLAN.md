# One-Day Execution Plan (Task-wise) for Gossip P2P Assignment

Date: 25 Feb 2026  
Target: Complete implementation, testing, and submission-ready packaging in **1 day**.

---

## 0) Final Deliverables (must exist before submission)

- `seed.py`
- `peer.py`
- `config.txt` (or `config.csv`)
- `README.md`
- `outputfile.txt` (seed/peer logs can append here or separate logs referenced in README)
- Compressed archive: `rollno1-rollno2.tar.gz`

---

## 1) One-Day Schedule (Hour-by-Hour)

## 09:00–09:45 — Task A: Project setup + protocol freeze

**Goals**
- Freeze message formats, socket model, and data structures.
- Create file skeletons and shared constants.

**Work items**
1. Create base files: `seed.py`, `peer.py`, `config.txt`, `README.md`.
2. Define message types (JSON over TCP with newline delimiter):
   - `REGISTER_REQ`, `REGISTER_VOTE`, `REGISTER_COMMIT`
   - `PEER_LIST_REQ`, `PEER_LIST_RESP`
   - `GOSSIP_MSG`
   - `SUSPECT_REQ`, `SUSPECT_ACK`, `DEAD_REPORT`
   - `SEED_DEAD_VOTE`, `SEED_DEAD_COMMIT`
3. Fix required output formats exactly for assignment text:
   - Gossip: `<self.timestamp>:<self.IP>:<self.Msg#>`
   - Dead report: `Dead Node:<DeadNode.IP>:<DeadNode.Port>:<self.timestamp>:<self.IP>`
4. Decide defaults:
   - gossip every 5 sec, max 10 messages/peer
   - seed quorum = `floor(n/2)+1`
   - peer-level dead threshold = majority of known neighbors of suspected node (or minimum 2 confirmations if neighbor list incomplete)

**Done when**
- All team members agree on protocol and edge-case behavior.

---

## 09:45–11:30 — Task B: Implement Seed Node core (`seed.py`)

**Goals**
- Seed starts TCP server, stores peer list (PL), and handles consensus for registration/removal.

**Work items**
1. Implement seed config parsing (self identity + seed list).
2. Maintain in-memory state:
   - `peer_list` (set of `ip:port`)
   - `pending_registrations` (proposal_id -> votes)
   - `pending_removals` (dead_node -> reports/votes)
3. Registration flow:
   - On `REGISTER_REQ` from peer: create proposal, send vote request to other seeds.
   - Count votes; if quorum reached -> commit in `peer_list`; reply success.
4. Peer list serving:
   - On `PEER_LIST_REQ`: send current PL.
5. Dead-node flow:
   - Accept `DEAD_REPORT` from peers.
   - Run seed-side vote exchange and quorum commit.
   - On commit: remove from PL and log globally.
6. Logging:
   - Console + append to `outputfile.txt`.
   - Log proposals, vote counts, commit/abort outcome.

**Done when**
- One seed can accept/register peers and respond with PL.
- Multi-seed vote messages exchange without crash.

---

## 11:30–13:30 — Task C: Implement Peer bootstrap + overlay formation (`peer.py`)

**Goals**
- Peer registers with quorum seeds, gets union PL, picks neighbors with power-law-like policy, and connects.

**Work items**
1. Parse `config.txt`, randomly choose at least quorum seeds.
2. Send `REGISTER_REQ` to selected seeds; proceed only if quorum success.
3. Request PL from connected seeds and compute union.
4. Build explicit topology representation:
   - `topology = {node: set(neighbors)}` (local view)
   - `neighbors = set(connected peer endpoints)`
5. Neighbor selection (power-law approximation in one-day scope):
   - Preferential attachment heuristic:
     - Higher probability for nodes already having higher observed degree.
     - Add random edges for connectivity.
   - Ensure at least 2 neighbors if available.
6. Establish TCP links to selected neighbors (bidirectional handling).

**Done when**
- New peer joins, gets non-empty neighbors, and topology state is populated.

---

## 13:30–14:15 — Break + quick code cleanup

- Resolve obvious bugs, unify function signatures, run formatting.

---

## 14:15–16:00 — Task D: Gossip dissemination + duplicate suppression

**Goals**
- Send periodic gossip and prevent forwarding duplicates.

**Work items**
1. Gossip generator timer: every 5 sec, up to 10 messages.
2. Construct exact format: `<timestamp>:<peer_ip>:<msg_no>`.
3. Message list (`ML`) for de-dup:
   - `seen_hashes` set, optional metadata map.
4. Forwarding rule:
   - First time: log + forward to all neighbors except sender.
   - Duplicate: drop silently.
5. Fault tolerance:
   - On send failure, mark neighbor as suspect candidate (not dead yet).

**Done when**
- 3+ peers receive each gossip once-first and duplicates are suppressed.

---

## 16:00–18:00 — Task E: Two-level liveness consensus (peer-level + seed-level)

**Goals**
- Implement robust dead-node detection with no unilateral action.

**Work items**
1. Peer-level suspicion:
   - Periodically ping neighbors (system `ping` command).
   - If missed threshold reached -> open suspicion window.
2. Peer-level confirmation:
   - Ask other neighbors of suspected node for confirmation (`SUSPECT_REQ`).
   - Generate report only if majority confirmations received.
3. Send final dead report to all connected seeds:
   - `Dead Node:<ip>:<port>:<timestamp>:<selfIP>`
4. Seed-level consensus:
   - Seeds share dead reports/votes.
   - Remove node only after quorum commit.
5. Propagation:
   - Seeds optionally notify peers to refresh local topology.

**Done when**
- Killing one peer triggers majority-based report and quorum-based seed removal.

---

## 18:00–19:30 — Task F: Integration testing matrix

**Goals**
- Validate all required assignment behaviors with logs.

**Test cases**
1. **Registration quorum success**: peer joins with >= quorum seeds.
2. **Registration quorum failure**: less than quorum -> rejected.
3. **Gossip spread**: each peer sends 10 messages, propagation observed.
4. **Duplicate suppression**: loops do not spam output.
5. **False suspicion resistance**: one peer accusation alone does not remove node.
6. **True failure removal**: majority peer confirmations + seed quorum removes node.
7. **Recovery handling (optional bonus)**: restarted node re-registers as fresh member.

**Done when**
- All tests have PASS/FAIL notes in `README.md`.

---

## 19:30–20:30 — Task G: Documentation + submission packaging

**Goals**
- Make project submission-ready and reproducible.

**Work items**
1. Write `README.md` with:
   - architecture
   - protocol messages
   - exact run steps (order: start seeds -> start peers)
   - testing procedure and sample commands
2. Verify naming rules exactly match assignment.
3. Ensure only config changes needed after submission.
4. Create tarball:
   - `tar -czf rollno1-rollno2.tar.gz seed.py peer.py config.txt README.md outputfile.txt`

**Done when**
- Another person can run from README with no verbal help.

---

## 2) Task Ownership Split (2-member team)

## Member 1 (Core Networking)
- Seed server concurrency, quorum voting logic, persistent logging, PL management.

## Member 2 (Peer Logic)
- Bootstrap, topology + neighbor selection, gossip engine, liveness checks, suspicion protocol.

## Shared
- Message schema definitions, integration tests, README, final packaging.

---

## 3) Minimal Technical Design (fast but complete)

## Socket model
- TCP sockets with one thread per incoming connection + synchronized shared state (locks).

## Serialization
- JSON messages, newline-delimited framing.

## State storage
- In-memory dictionaries/sets (assignment does not require DB).

## Topology representation (explicit, required)
- Each peer keeps local adjacency map:
  - own neighbors
  - observed neighbor degree snapshots from handshake metadata
- Use this to apply preferential attachment when selecting new neighbors.

---

## 4) Security/Attack Discussion Points (for viva)

1. **Single malicious peer false report**: blocked by peer-level majority confirmation.
2. **Single malicious seed deletion**: blocked by seed-level quorum.
3. **Colluding peers**: reduced impact by requiring majority among actual neighbors and seed corroboration.
4. **Sybil-like bogus registrations**: mitigated by multi-seed agreement and duplicate endpoint checks.
5. **Replay of old dead reports**: include timestamps/proposal IDs and expiration windows.

---

## 5) Fast Command Checklist (example run)

1. Start seeds (separate terminals):
   - `python3 seed.py --id 1 --ip 127.0.0.1 --port 5001 --config config.txt`
   - `python3 seed.py --id 2 --ip 127.0.0.1 --port 5002 --config config.txt`
   - `python3 seed.py --id 3 --ip 127.0.0.1 --port 5003 --config config.txt`
2. Start peers:
   - `python3 peer.py --ip 127.0.0.1 --port 6001 --config config.txt`
   - `python3 peer.py --ip 127.0.0.1 --port 6002 --config config.txt`
   - ...
3. Simulate failure:
   - Kill one peer process; observe suspicion -> consensus -> removal logs.

---

## 6) Day-Of Priority Rules (to finish in time)

1. First make registration + gossip stable.
2. Then add peer-level suspicion and seed-level removal consensus.
3. Keep protocol simple and deterministic; avoid over-engineering.
4. If running out of time, prioritize correctness of quorum checks + logs over advanced optimizations.

---

## 7) Final Pre-Submission Checklist

- [ ] Peer connects to at least `floor(n/2)+1` seeds.
- [ ] Seed never adds/removes peer without quorum.
- [ ] Peer never reports dead on single failed ping.
- [ ] Gossip interval 5 sec, max 10 messages per peer.
- [ ] Duplicate gossip suppression works.
- [ ] Console and file logging present.
- [ ] README includes compile/run/testing instructions.
- [ ] Archive named exactly `rollno1-rollno2.tar.gz`.

---

If you want, next I can generate a **ready-to-run starter code skeleton** for `seed.py`, `peer.py`, and `config.txt` following this exact plan so you can begin implementation immediately.