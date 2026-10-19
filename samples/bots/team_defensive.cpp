// OTT v1 defensive bot. C++17.
// Reads one observation line and writes one legal action line.
#include <bits/stdc++.h>
using namespace std;

struct Json {
    enum Type { Null, Bool, Num, Str, Arr, Obj } type = Null;
    bool b = false;
    double n = 0;
    string s;
    vector<Json> a;
    vector<pair<string, Json>> o;
    const Json& operator[](const string& k) const {
        static const Json nul;
        for (const auto& p : o) if (p.first == k) return p.second;
        return nul;
    }
    bool has(const string& k) const {
        for (const auto& p : o) if (p.first == k) return true;
        return false;
    }
};

struct Parser {
    const string& text;
    size_t i = 0;
    explicit Parser(const string& s) : text(s) {}
    void ws() { while (i < text.size() && isspace((unsigned char)text[i])) ++i; }
    string str() {
        string out;
        ++i;
        while (i < text.size() && text[i] != '"') {
            if (text[i] == '\\' && i + 1 < text.size()) {
                ++i;
                char c = text[i];
                if (c == 'n') out += '\n';
                else if (c == 't') out += '\t';
                else if (c == 'u') { out += '?'; i += 4; }
                else out += c;
            } else out += text[i];
            ++i;
        }
        if (i < text.size()) ++i;
        return out;
    }
    Json value() {
        ws();
        Json j;
        if (i >= text.size()) return j;
        if (text[i] == '{') {
            j.type = Json::Obj; ++i; ws();
            while (i < text.size() && text[i] != '}') {
                ws(); string key = str(); ws(); ++i;
                j.o.emplace_back(key, value()); ws();
                if (i < text.size() && text[i] == ',') ++i;
                ws();
            }
            if (i < text.size()) ++i;
        } else if (text[i] == '[') {
            j.type = Json::Arr; ++i; ws();
            while (i < text.size() && text[i] != ']') {
                j.a.push_back(value()); ws();
                if (i < text.size() && text[i] == ',') ++i;
                ws();
            }
            if (i < text.size()) ++i;
        } else if (text[i] == '"') {
            j.type = Json::Str; j.s = str();
        } else if (text.compare(i, 4, "true") == 0) {
            j.type = Json::Bool; j.b = true; i += 4;
        } else if (text.compare(i, 5, "false") == 0) {
            j.type = Json::Bool; i += 5;
        } else if (text.compare(i, 4, "null") == 0) {
            i += 4;
        } else {
            j.type = Json::Num;
            size_t end = i;
            while (end < text.size() && (isdigit((unsigned char)text[end]) || strchr("+-.eE", text[end]))) ++end;
            if (end > i) { j.n = stod(text.substr(i, end - i)); i = end; }
        }
        return j;
    }
};

struct Piece { string side, piece, promotion; };
struct Action { string type, piece, from, to, promotion; };
struct Obs {
    string phase, side;
    map<string, Piece> board;
    vector<Action> legal;
};
using Board = map<string, Piece>;

static int fileOf(const string& sq) { return sq[0] - 'a'; }
static int rankOf(const string& sq) { return sq[1] - '1'; }
static string otherSide(const string& side) { return side == "white" ? "black" : "white"; }
static bool inside(int f, int r) { return f >= 0 && f < 9 && r >= 0 && r < 9; }
static string squareAt(int f, int r) { return string(1, char('a' + f)) + char('1' + r); }

static Obs parseObs(const Json& j) {
    Obs obs;
    obs.phase = j["phase"].s;
    obs.side = j["side"].s;
    for (const auto& p : j["board"].o) {
        const Json& v = p.second;
        obs.board[p.first] = Piece{v["side"].s, v["piece"].s,
                                   v["promotion"].type == Json::Str ? v["promotion"].s : ""};
    }
    for (const Json& a : j["legal_actions"].a) {
        obs.legal.push_back(Action{a["type"].s, a["piece"].s, a["from"].s,
                                   a["to"].s, a.has("promotion") ? a["promotion"].s : ""});
    }
    return obs;
}

static string toJson(const Action& a) {
    if (a.type == "place")
        return "{\"type\":\"place\",\"piece\":\"" + a.piece + "\",\"to\":\"" + a.to + "\"}";
    string out = "{\"type\":\"move\",\"from\":\"" + a.from + "\",\"to\":\"" + a.to + "\"";
    if (!a.promotion.empty()) out += ",\"promotion\":\"" + a.promotion + "\"";
    return out + "}";
}

static int pieceValue(const Piece& p) {
    if (p.piece == "king") return 100000;
    return 100 + (p.promotion.empty() ? 0 : 35);
}

static bool beats(const string& attacker, const string& target) {
    return (attacker == "scissors" && target == "paper") ||
           (attacker == "paper" && target == "hammer") ||
           (attacker == "hammer" && target == "scissors");
}

static bool canCapture(const Piece& attacker, const Piece& target) {
    if (target.piece == "king") return true;
    return beats(attacker.piece, target.piece) ||
           (!attacker.promotion.empty() && attacker.piece == target.piece);
}

static bool clearRay(const Board& board, int f, int r, int df, int dr, int steps) {
    for (int k = 1; k < steps; ++k) {
        if (board.count(squareAt(f + df * k, r + dr * k))) return false;
    }
    return true;
}

// True when piece at from can capture target square under OTT geometry and RPS rules.
static bool reachesCapture(const Board& board, const string& from, const string& to) {
    auto ai = board.find(from), ti = board.find(to);
    if (ai == board.end() || ti == board.end()) return false;
    const Piece& attacker = ai->second;
    const Piece& target = ti->second;
    if (attacker.side == target.side || attacker.piece == "king" || !canCapture(attacker, target)) return false;

    int df = fileOf(to) - fileOf(from), dr = rankOf(to) - rankOf(from);
    int af = abs(df), ar = abs(dr);
    if (attacker.promotion.empty()) {
        int forward = attacker.side == "white" ? 1 : -1;
        return dr == forward && af <= 1;
    }

    if (af <= 1 && ar <= 1 && (af + ar > 0)) return true;
    bool rookRay = attacker.promotion == "rook" && ((df == 0) != (dr == 0));
    bool bishopRay = attacker.promotion == "bishop" && af == ar && af > 0;
    if (!rookRay && !bishopRay) return false;
    int steps = max(af, ar);
    return clearRay(board, fileOf(from), rankOf(from), (df == 0 ? 0 : df / af),
                    (dr == 0 ? 0 : dr / ar), steps);
}

static vector<string> attackersTo(const Board& board, const string& side, const string& target) {
    vector<string> out;
    for (const auto& p : board) {
        if (p.second.side == side && reachesCapture(board, p.first, target)) out.push_back(p.first);
    }
    return out;
}

static Board afterMove(const Board& board, const Action& a) {
    Board next = board;
    auto it = next.find(a.from);
    if (it == next.end()) return next;
    Piece moving = it->second;
    next.erase(it);
    if (!a.promotion.empty()) moving.promotion = a.promotion;
    next.erase(a.to);
    next[a.to] = moving;
    return next;
}

// Estimate loss from best immediate enemy capture, including a possible recapture.
static double immediateRisk(const Board& board, const string& defendingSide) {
    const string enemy = otherSide(defendingSide);
    double worstLoss = 0.0;
    for (const auto& targetEntry : board) {
        if (targetEntry.second.side != defendingSide) continue;
        const string& targetSq = targetEntry.first;
        const Piece targetPiece = targetEntry.second;
        vector<string> attackers = attackersTo(board, enemy, targetSq);
        for (const string& attackerSq : attackers) {
            const Piece attacker = board.at(attackerSq);
            if (targetPiece.piece == "king") return 10000000.0;

            Board afterCapture = board;
            afterCapture.erase(attackerSq);
            afterCapture.erase(targetSq);
            afterCapture[targetSq] = attacker;
            bool canRecapture = !attackersTo(afterCapture, defendingSide, targetSq).empty();
            double loss = pieceValue(targetPiece);
            if (canRecapture) loss = max(0.0, loss - 0.82 * pieceValue(attacker));
            worstLoss = max(worstLoss, loss);
        }
    }
    return worstLoss;
}

static double placementScore(const Obs& obs, const Action& a) {
    const int f = fileOf(a.to), r = rankOf(a.to);
    const int homeBack = obs.side == "white" ? 0 : 8;
    const int homeFront = obs.side == "white" ? 1 : 7;
    if (a.piece == "king") {
        // Keep king on back edge. Corner squares expose fewer approach directions.
        double score = (r == homeBack ? 50.0 : 0.0);
        if (f == 0 || f == 8) score += 35.0;
        else if (f == 1 || f == 7) score += 12.0;
        score -= abs(f - 4) * 0.5;
        return score;
    }

    double score = (r == homeFront ? 22.0 : 5.0);
    score += 8.0 - 2.0 * abs(f - 4); // central files support both wings
    if (r == homeBack && (f == 0 || f == 8)) score -= 32.0; // save corner for king

    // Spread pieces across files and rows; avoid a single crowded cluster.
    double nearest = 20.0;
    for (const auto& p : obs.board) {
        if (p.second.side != obs.side) continue;
        int df = abs(f - fileOf(p.first)), dr = abs(r - rankOf(p.first));
        nearest = min(nearest, double(df + dr));
    }
    if (!obs.board.empty()) score += min(8.0, nearest * 1.4);

    // Keep different RPS pieces interleaved so no single counter wins a whole wing.
    for (const auto& p : obs.board) {
        if (p.second.side != obs.side || p.second.piece == a.piece) continue;
        if (abs(f - fileOf(p.first)) <= 1) score += 2.0;
    }
    return score;
}

static double moveScore(const Obs& obs, const Action& a) {
    Board next = afterMove(obs.board, a);
    double score = 0.0;
    auto target = obs.board.find(a.to);
    if (target != obs.board.end() && target->second.side != obs.side) {
        score += target->second.piece == "king" ? 10000000.0 : 1.15 * pieceValue(target->second);
    }
    if (!a.promotion.empty()) score += 45.0 + (a.promotion == "rook" ? 8.0 : 0.0);

    auto mover = next.find(a.to);
    if (mover != next.end()) {
        if (mover->second.piece == "king") {
            // Prefer king moves toward board edge when danger is equal.
            int edgeDistance = min(min(fileOf(a.to), 8 - fileOf(a.to)),
                                   min(rankOf(a.to), 8 - rankOf(a.to)));
            score -= edgeDistance * 0.8;
        } else if (mover->second.promotion.empty()) {
            int advance = rankOf(a.to) - rankOf(a.from);
            score += (obs.side == "white" ? advance : -advance) * 1.7;
        }
    }

    double risk = immediateRisk(next, obs.side);
    score -= risk * 1.35;

    // Small reward for attacks that survive immediate tactical reply.
    double bestThreat = 0.0;
    for (const auto& p : next) {
        if (p.second.side == obs.side) continue;
        for (const string& attackerSq : attackersTo(next, obs.side, p.first)) {
            const Piece attacker = next.at(attackerSq);
            Board afterCapture = next;
            afterCapture.erase(attackerSq);
            afterCapture.erase(p.first);
            afterCapture[p.first] = attacker;
            bool recaptured = !attackersTo(afterCapture, otherSide(obs.side), p.first).empty();
            double gain = pieceValue(p.second);
            if (recaptured) gain = max(0.0, gain - 0.75 * pieceValue(attacker));
            bestThreat = max(bestThreat, gain);
        }
    }
    score += min(18.0, bestThreat * 0.08);
    return score;
}

static size_t choose(const Obs& obs) {
    if (obs.phase == "placement") {
        bool nonKingAvailable = false;
        for (const Action& a : obs.legal) if (a.piece != "king") nonKingAvailable = true;
        double best = -numeric_limits<double>::infinity();
        size_t bestIndex = 0;
        for (size_t i = 0; i < obs.legal.size(); ++i) {
            const Action& a = obs.legal[i];
            // Place king last, after all nine moving pieces.
            if (a.piece == "king" && nonKingAvailable) continue;
            double score = placementScore(obs, a);
            if (score > best) { best = score; bestIndex = i; }
        }
        return bestIndex;
    }

    double best = -numeric_limits<double>::infinity();
    size_t bestIndex = 0;
    for (size_t i = 0; i < obs.legal.size(); ++i) {
        double score = moveScore(obs, obs.legal[i]);
        if (score > best) { best = score; bestIndex = i; }
    }
    return bestIndex;
}

int main() {
    ios::sync_with_stdio(false);
    string line;
    while (getline(cin, line)) {
        if (line.empty()) continue;
        Parser parser(line);
        Obs obs = parseObs(parser.value());
        if (obs.legal.empty()) break;
        cout << toJson(obs.legal[choose(obs)]) << '\n' << flush;
    }
    return 0;
}
