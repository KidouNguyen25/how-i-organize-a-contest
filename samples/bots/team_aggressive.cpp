// OTT v1 bot template (C++17).
// Engine gửi mỗi lượt MỘT dòng JSON (observation) qua stdin; bot trả MỘT dòng JSON (action) qua stdout.
// Chỉ sửa hàm choose() giữa hai dòng YOUR STRATEGY. Giữ nguyên phần còn lại.
// stdout chỉ dành cho action; ghi chẩn đoán vào stderr (cerr).
#include <bits/stdc++.h>
using namespace std;

// ---------- JSON tối giản ----------
struct Json {
    enum Type { Null, Bool, Num, Str, Arr, Obj } type = Null;
    bool b = false;
    double n = 0;
    string s;
    vector<Json> a;
    vector<pair<string, Json>> o;
    const Json& operator[](const string& k) const {
        static const Json nul;
        for (auto& p : o) if (p.first == k) return p.second;
        return nul;
    }
    bool has(const string& k) const { for (auto& p : o) if (p.first == k) return true; return false; }
};
struct Parser {
    const string& t;
    size_t i = 0;
    explicit Parser(const string& text) : t(text) {}
    void ws() { while (i < t.size() && isspace((unsigned char)t[i])) i++; }
    string str() {
        string r;
        i++;  // opening quote
        while (i < t.size() && t[i] != '"') {
            if (t[i] == '\\' && i + 1 < t.size()) {
                i++;
                char c = t[i];
                if (c == 'n') r += '\n'; else if (c == 't') r += '\t';
                else if (c == 'u') { r += '?'; i += 4; }
                else r += c;
            } else r += t[i];
            i++;
        }
        i++;  // closing quote
        return r;
    }
    Json value() {
        ws();
        Json j;
        if (t[i] == '{') {
            j.type = Json::Obj; i++; ws();
            while (t[i] != '}') {
                ws(); string k = str(); ws(); i++;  // ':'
                j.o.emplace_back(k, value()); ws();
                if (t[i] == ',') i++;
                ws();
            }
            i++;
        } else if (t[i] == '[') {
            j.type = Json::Arr; i++; ws();
            while (t[i] != ']') {
                j.a.push_back(value()); ws();
                if (t[i] == ',') i++;
                ws();
            }
            i++;
        } else if (t[i] == '"') { j.type = Json::Str; j.s = str(); }
        else if (t.compare(i, 4, "true") == 0) { j.type = Json::Bool; j.b = true; i += 4; }
        else if (t.compare(i, 5, "false") == 0) { j.type = Json::Bool; i += 5; }
        else if (t.compare(i, 4, "null") == 0) { i += 4; }
        else { j.type = Json::Num; size_t e = i; while (e < t.size() && (isdigit((unsigned char)t[e]) || strchr("+-.eE", t[e]))) e++; j.n = stod(t.substr(i, e - i)); i = e; }
        return j;
    }
};

// ---------- Dữ liệu ván ----------
struct Piece { string side, piece, promotion; };            // piece: scissors|hammer|paper|king ; promotion: ""|rook|bishop
struct Action { string type, piece, from, to, promotion; };  // type: place|move
struct Obs {
    string phase, side, match_id;                            // phase: placement|play ; side: white|black
    map<string, Piece> board;                                // key: "a1".."i9"
    vector<Action> legal;                                    // các action hợp lệ — chỉ được chọn trong đây
    long long myClockMs = 0, oppClockMs = 0;
};
static mt19937 rng((unsigned)chrono::steady_clock::now().time_since_epoch().count());
[[maybe_unused]] static int rankOf(const string& sq) { return sq[1] - '1'; }  // 0..8
[[maybe_unused]] static int fileOf(const string& sq) { return sq[0] - 'a'; }  // 0..8
[[maybe_unused]] static bool isCapture(const Obs& o, const Action& a) { return a.type == "move" && o.board.count(a.to) && o.board.at(a.to).side != o.side; }
[[maybe_unused]] static int forward(const Obs& o, const Action& a) { int d = rankOf(a.to) - rankOf(a.from); return o.side == "white" ? d : -d; }

// ---------- YOUR STRATEGY BEGIN ----------
static bool beats(const string& attacker, const string& target) {
    return (attacker == "scissors" && target == "paper") ||
           (attacker == "paper" && target == "hammer") ||
           (attacker == "hammer" && target == "scissors");
}

static int pieceValue(const Piece& p) {
    if (p.piece == "king") return 30000;
    if (p.promotion == "rook") return 310;
    if (p.promotion == "bishop") return 250;
    return 100;
}

static bool canTakeType(const Piece& attacker, const Piece& target) {
    if (attacker.piece == "king") return false;
    if (target.piece == "king") return true;
    if (!attacker.promotion.empty() && attacker.piece == target.piece) return true;
    return beats(attacker.piece, target.piece);
}

static bool clearLine(const map<string, Piece>& board, const string& from, const string& to) {
    int dx = fileOf(to) - fileOf(from), dy = rankOf(to) - rankOf(from);
    int sx = (dx > 0) - (dx < 0), sy = (dy > 0) - (dy < 0);
    int x = fileOf(from) + sx, y = rankOf(from) + sy;
    while (x != fileOf(to) || y != rankOf(to)) {
        string sq = string(1, char('a' + x)) + char('1' + y);
        if (board.count(sq)) return false;
        x += sx;
        y += sy;
    }
    return true;
}

static bool canCaptureAt(const map<string, Piece>& board, const string& from,
                         const Piece& attacker, const string& to, const Piece& target) {
    if (!canTakeType(attacker, target)) return false;
    int dx = fileOf(to) - fileOf(from), dy = rankOf(to) - rankOf(from);
    int ax = abs(dx), ay = abs(dy);
    if (ax == 0 && ay == 0) return false;
    if (attacker.promotion.empty()) {
        int fwd = attacker.side == "white" ? 1 : -1;
        return dy == fwd && ax <= 1;
    }
    if (max(ax, ay) == 1) return true;
    bool rookRay = attacker.promotion == "rook" && (dx == 0 || dy == 0);
    bool bishopRay = attacker.promotion == "bishop" && ax == ay;
    return (rookRay || bishopRay) && clearLine(board, from, to);
}

static int attackersTo(const map<string, Piece>& board, const string& side,
                       const string& targetSquare, const Piece& target) {
    int count = 0;
    for (const auto& entry : board) {
        const Piece& p = entry.second;
        if (p.side != side) continue;
        if (canCaptureAt(board, entry.first, p, targetSquare, target)) count++;
    }
    return count;
}

static int attackPressure(const map<string, Piece>& board, const string& side) {
    int score = 0;
    for (const auto& targetEntry : board) {
        const Piece& target = targetEntry.second;
        if (target.side == side) continue;
        int n = attackersTo(board, side, targetEntry.first, target);
        if (n == 0) continue;
        if (target.piece == "king") score += 420 + min(n - 1, 2) * 40;
        else score += 34 + (target.promotion.empty() ? 0 : 18) + min(n - 1, 2) * 12;
    }
    return score;
}

static bool kingThreatened(const map<string, Piece>& board, const string& side) {
    for (const auto& entry : board) {
        if (entry.second.side == side && entry.second.piece == "king")
            return attackersTo(board, side == "white" ? "black" : "white", entry.first, entry.second) > 0;
    }
    return false;
}

static int placementScore(const Obs& obs, const Action& a) {
    int file = fileOf(a.to), rank = rankOf(a.to);
    bool white = obs.side == "white";
    int frontRank = white ? 1 : 7;
    int backRank = white ? 0 : 8;
    int centerBonus = 12 - 3 * abs(file - 4);
    int score = centerBonus;

    if (a.piece == "king") {
        score += (rank == backRank ? 70 : -15);
        score += 5 * (4 - abs(file - 4));
    } else {
        score += (rank == frontRank ? 55 : 8);
        static const string plan[9] = {
            "paper", "scissors", "hammer", "paper", "scissors",
            "hammer", "paper", "scissors", "hammer"
        };
        if (rank == frontRank && plan[file] == a.piece) score += 28;
        for (const auto& entry : obs.board) {
            if (entry.second.side == obs.side && fileOf(entry.first) == file)
                score -= 7;
        }
    }
    return score + int(rng() % 5);
}

// Return legal action index. Favor captures, forward pressure, and promotion.
size_t choose(const Obs& obs) {
    if (obs.legal.empty()) return 0;
    if (obs.phase == "placement") {
        size_t best = 0;
        int bestScore = numeric_limits<int>::min();
        for (size_t i = 0; i < obs.legal.size(); ++i) {
            int score = placementScore(obs, obs.legal[i]);
            if (score > bestScore) bestScore = score, best = i;
        }
        return best;
    }

    const string enemy = obs.side == "white" ? "black" : "white";
    int beforePressure = attackPressure(obs.board, obs.side);
    bool kingWasThreatened = kingThreatened(obs.board, obs.side);
    size_t best = 0;
    int bestScore = numeric_limits<int>::min();

    for (size_t i = 0; i < obs.legal.size(); ++i) {
        const Action& a = obs.legal[i];
        auto ownIt = obs.board.find(a.from);
        if (ownIt == obs.board.end()) continue;
        Piece mover = ownIt->second;
        map<string, Piece> next = obs.board;
        auto targetIt = next.find(a.to);
        int score = 0;

        if (targetIt != next.end() && targetIt->second.side == enemy) {
            if (targetIt->second.piece == "king") score += 1000000;
            else score += 300 + 2 * pieceValue(targetIt->second);
            next.erase(targetIt);
        }

        int advance = forward(obs, a);
        score += advance * 32;
        int fromCenter = abs(fileOf(a.from) - 4) + abs(rankOf(a.from) - 4);
        int toCenter = abs(fileOf(a.to) - 4) + abs(rankOf(a.to) - 4);
        score += (fromCenter - toCenter) * 3;
        if (!a.promotion.empty()) score += a.promotion == "rook" ? 420 : 330;

        next.erase(a.from);
        next[a.to] = mover;
        int afterPressure = attackPressure(next, obs.side);
        score += afterPressure - beforePressure;

        int threats = attackersTo(next, enemy, a.to, mover);
        if (threats > 0) {
            bool recapturable = false;
            for (const auto& threatener : next) {
                if (threatener.second.side != enemy ||
                    !canCaptureAt(next, threatener.first, threatener.second, a.to, mover))
                    continue;
                for (const auto& defender : next) {
                    if (defender.second.side == obs.side && defender.first != a.to &&
                        canCaptureAt(next, defender.first, defender.second, a.to,
                                     threatener.second)) {
                        recapturable = true;
                        break;
                    }
                }
                if (recapturable) break;
            }
            int loss = pieceValue(mover) * 9 / 10 + 35;
            if (recapturable && mover.piece != "king") loss = loss * 55 / 100;
            score -= loss + min(threats - 1, 3) * 22;
        }

        bool kingIsThreatened = kingThreatened(next, obs.side);
        if (kingIsThreatened) score -= 30000;
        else if (kingWasThreatened) score += 30000;

        score += int(rng() % 7);
        if (score > bestScore) bestScore = score, best = i;
    }
    return best;
}
// ---------- YOUR STRATEGY END ----------

static Obs parseObs(const Json& j) {
    Obs o;
    o.phase = j["phase"].s; o.side = j["side"].s; o.match_id = j["match_id"].s;
    for (auto& p : j["board"].o) o.board[p.first] = Piece{p.second["side"].s, p.second["piece"].s, p.second["promotion"].type == Json::Str ? p.second["promotion"].s : ""};
    for (auto& a : j["legal_actions"].a)
        o.legal.push_back(Action{a["type"].s, a["piece"].s, a["from"].s, a["to"].s, a.has("promotion") ? a["promotion"].s : ""});
    o.myClockMs = (long long)j["clocks_ms"][o.side].n;
    o.oppClockMs = (long long)j["clocks_ms"][o.side == "white" ? "black" : "white"].n;
    return o;
}
static string toJson(const Action& a) {
    if (a.type == "place") return "{\"type\":\"place\",\"piece\":\"" + a.piece + "\",\"to\":\"" + a.to + "\"}";
    string r = "{\"type\":\"move\",\"from\":\"" + a.from + "\",\"to\":\"" + a.to + "\"";
    if (!a.promotion.empty()) r += ",\"promotion\":\"" + a.promotion + "\"";
    return r + "}";
}
int main() {
    ios::sync_with_stdio(false);
    string line;
    while (getline(cin, line)) {
        if (line.empty()) continue;
        Parser p(line);
        Obs obs = parseObs(p.value());
        if (obs.legal.empty()) break;
        cout << toJson(obs.legal[choose(obs)]) << endl;  // endl = flush
    }
    return 0;
}
