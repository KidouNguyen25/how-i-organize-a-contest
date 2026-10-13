// OTT v1 bot: positional strategy for center control, safe development, and promotion.
#include <bits/stdc++.h>
using namespace std;

struct Json {
    enum Type { Null, Bool, Num, Str, Arr, Obj } type = Null;
    bool b = false;
    double n = 0;
    string s;
    vector<Json> a;
    vector<pair<string, Json>> o;
    const Json& operator[](const string& key) const {
        static const Json empty;
        for (const auto& item : o) if (item.first == key) return item.second;
        return empty;
    }
    bool has(const string& key) const {
        for (const auto& item : o) if (item.first == key) return true;
        return false;
    }
};

struct Parser {
    const string& text;
    size_t pos = 0;
    explicit Parser(const string& input) : text(input) {}
    void ws() { while (pos < text.size() && isspace((unsigned char)text[pos])) ++pos; }
    string str() {
        string result;
        ++pos;
        while (pos < text.size() && text[pos] != '"') {
            if (text[pos] == '\\' && pos + 1 < text.size()) {
                ++pos;
                char c = text[pos];
                if (c == 'n') result += '\n';
                else if (c == 't') result += '\t';
                else if (c == 'u') { result += '?'; pos += 4; }
                else result += c;
            } else result += text[pos];
            ++pos;
        }
        ++pos;
        return result;
    }
    Json value() {
        ws();
        Json result;
        if (text[pos] == '{') {
            result.type = Json::Obj;
            ++pos; ws();
            while (text[pos] != '}') {
                ws(); string key = str(); ws(); ++pos;
                result.o.emplace_back(key, value()); ws();
                if (text[pos] == ',') ++pos;
                ws();
            }
            ++pos;
        } else if (text[pos] == '[') {
            result.type = Json::Arr;
            ++pos; ws();
            while (text[pos] != ']') {
                result.a.push_back(value()); ws();
                if (text[pos] == ',') ++pos;
                ws();
            }
            ++pos;
        } else if (text[pos] == '"') {
            result.type = Json::Str; result.s = str();
        } else if (text.compare(pos, 4, "true") == 0) {
            result.type = Json::Bool; result.b = true; pos += 4;
        } else if (text.compare(pos, 5, "false") == 0) {
            result.type = Json::Bool; pos += 5;
        } else if (text.compare(pos, 4, "null") == 0) {
            pos += 4;
        } else {
            result.type = Json::Num;
            size_t end = pos;
            while (end < text.size() && (isdigit((unsigned char)text[end]) || strchr("+-.eE", text[end]))) ++end;
            result.n = stod(text.substr(pos, end - pos));
            pos = end;
        }
        return result;
    }
};

struct Piece { string side, piece, promotion; };
struct Action { string type, piece, from, to, promotion; };
struct HistoryEntry { string side; Action action; };
struct Obs {
    string phase, side;
    map<string, Piece> board;
    vector<Action> legal;
    vector<HistoryEntry> history;
};

static Action readAction(const Json& j) {
    return Action{j["type"].s, j["piece"].s, j["from"].s, j["to"].s,
                  j.has("promotion") ? j["promotion"].s : ""};
}

static Obs parseObs(const Json& j) {
    Obs obs;
    obs.phase = j["phase"].s;
    obs.side = j["side"].s;
    for (const auto& item : j["board"].o) {
        const Json& value = item.second;
        obs.board[item.first] = Piece{value["side"].s, value["piece"].s,
                                      value["promotion"].type == Json::Str ? value["promotion"].s : ""};
    }
    for (const Json& value : j["legal_actions"].a) obs.legal.push_back(readAction(value));
    for (const Json& value : j["history"].a)
        obs.history.push_back(HistoryEntry{value["side"].s, readAction(value["action"])});
    return obs;
}

static int fileOf(const string& square) { return square[0] - 'a'; }
static int rankOf(const string& square) { return square[1] - '1'; }
static int forwardSign(const string& side) { return side == "white" ? 1 : -1; }
static int advanceOf(const string& side, int rank) { return side == "white" ? rank : 8 - rank; }
static int centerDistance(int file, int rank) { return max(abs(file - 4), abs(rank - 4)); }
static bool isEnemyCapture(const map<string, Piece>& board, const Action& action, const string& side) {
    auto found = board.find(action.to);
    return action.type == "move" && found != board.end() && found->second.side != side;
}

static bool beats(const string& attacker, const string& target) {
    return (attacker == "scissors" && target == "paper") ||
           (attacker == "paper" && target == "hammer") ||
           (attacker == "hammer" && target == "scissors");
}

static bool clearRay(const map<string, Piece>& board, int fromFile, int fromRank,
                     int toFile, int toRank) {
    int df = (toFile > fromFile) - (toFile < fromFile);
    int dr = (toRank > fromRank) - (toRank < fromRank);
    int file = fromFile + df, rank = fromRank + dr;
    while (file != toFile || rank != toRank) {
        string square(1, char('a' + file)); square += char('1' + rank);
        if (board.count(square)) return false;
        file += df; rank += dr;
    }
    return true;
}

static bool canCaptureAt(const map<string, Piece>& board, const string& origin,
                         const Piece& attacker, const string& targetSquare,
                         const Piece& target) {
    if (attacker.piece == "king") return false;
    if (target.side == attacker.side) return false;
    bool compatible = target.piece == "king" || beats(attacker.piece, target.piece) ||
                      (!attacker.promotion.empty() && attacker.piece == target.piece);
    if (!compatible) return false;

    int df = fileOf(targetSquare) - fileOf(origin);
    int dr = rankOf(targetSquare) - rankOf(origin);
    if (attacker.promotion.empty()) {
        return dr == forwardSign(attacker.side) && abs(df) <= 1;
    }
    if (max(abs(df), abs(dr)) == 1) return true;
    bool rookLine = attacker.promotion == "rook" && (df == 0 || dr == 0);
    bool bishopLine = attacker.promotion == "bishop" && abs(df) == abs(dr);
    return (rookLine || bishopLine) && clearRay(board, fileOf(origin), rankOf(origin),
                                                 fileOf(targetSquare), rankOf(targetSquare));
}

static double safetyScore(const map<string, Piece>& board, const string& side) {
    double score = 0;
    for (const auto& own : board) {
        if (own.second.side != side) continue;
        int attackers = 0;
        for (const auto& enemy : board) {
            if (enemy.second.side == side) continue;
            if (canCaptureAt(board, enemy.first, enemy.second, own.first, own.second)) ++attackers;
        }
        if (!attackers) continue;
        if (own.second.piece == "king") score -= 900.0 + 250.0 * (attackers - 1);
        else {
            double value = own.second.promotion.empty() ? 34.0 : 52.0;
            score -= value * min(attackers, 3);
        }
    }
    return score;
}

static double pressureScore(const map<string, Piece>& board, const string& side) {
    double score = 0;
    for (const auto& enemy : board) {
        if (enemy.second.side == side) continue;
        int attackers = 0;
        for (const auto& own : board) {
            if (own.second.side == side && canCaptureAt(board, own.first, own.second,
                                                        enemy.first, enemy.second)) ++attackers;
        }
        if (attackers) score += enemy.second.piece == "king" ? 120.0 : 7.0 * min(attackers, 2);
    }
    return score;
}

static double placementScore(const Obs& obs, const Action& action) {
    int file = fileOf(action.to), rank = rankOf(action.to);
    int depth = advanceOf(obs.side, rank);
    double score = 0;
    if (action.piece == "king") {
        score += 34.0 - 5.0 * abs(file - 4) - 24.0 * depth;
        for (const auto& item : obs.board) {
            if (item.second.side != obs.side) continue;
            int df = abs(file - fileOf(item.first));
            int dr = abs(rank - rankOf(item.first));
            if (df <= 1 && dr == 1 && advanceOf(obs.side, rankOf(item.first)) > depth)
                score += 2.0;
        }
    } else {
        score += 15.0 * depth;
        score += 4.0 * (4 - centerDistance(file, rank));
        for (const auto& item : obs.board) {
            if (item.second.side != obs.side) continue;
            int oldFile = fileOf(item.first), oldRank = rankOf(item.first);
            if (oldFile == file) score -= 2.2;
            if (oldRank == rank && abs(oldFile - file) == 1) score -= 1.4;
            if (oldRank == rank && abs(oldFile - file) > 2) score += 0.4;
        }
        if (depth == 1 && (file == 3 || file == 4 || file == 5)) score += 2.0;
    }
    return score;
}

static double moveScore(const Obs& obs, const Action& action) {
    auto moverIt = obs.board.find(action.from);
    if (moverIt == obs.board.end()) return -1e9;
    const Piece mover = moverIt->second;
    const Piece* captured = nullptr;
    auto targetIt = obs.board.find(action.to);
    if (targetIt != obs.board.end() && targetIt->second.side != obs.side) captured = &targetIt->second;

    double score = 0;
    if (captured) score += captured->piece == "king" ? 1000000.0 : 115.0;
    if (!action.promotion.empty()) score += action.promotion == "rook" ? 74.0 : 69.0;

    int fromFile = fileOf(action.from), fromRank = rankOf(action.from);
    int toFile = fileOf(action.to), toRank = rankOf(action.to);
    int advance = advanceOf(obs.side, toRank) - advanceOf(obs.side, fromRank);
    double centerChange = (4 - centerDistance(toFile, toRank)) -
                          (4 - centerDistance(fromFile, fromRank));
    if (mover.piece != "king") {
        score += 3.6 * centerChange;
        score += 3.2 * advance;
        int homeRank = obs.side == "white" ? 0 : 8;
        int forwardRank = obs.side == "white" ? 1 : 7;
        if (fromRank == homeRank && toRank != homeRank) score += 3.0;
        if (toRank == forwardRank && fromRank != forwardRank) score += 2.0;
    } else {
        score += 0.7 * centerChange;
        score -= max(0, advance) * 1.8;
    }

    map<string, Piece> after = obs.board;
    after.erase(action.from);
    after.erase(action.to);
    Piece moved = mover;
    if (!action.promotion.empty()) moved.promotion = action.promotion;
    after[action.to] = moved;

    score += safetyScore(after, obs.side);
    score += 0.9 * pressureScore(after, obs.side);

    int ownRecentMoves = 0;
    for (auto it = obs.history.rbegin(); it != obs.history.rend() && ownRecentMoves < 8; ++it) {
        if (it->side != obs.side || it->action.type != "move") continue;
        ++ownRecentMoves;
        if (action.from == it->action.to && action.to == it->action.from) score -= 24.0;
        else if (action.from == it->action.from && action.to == it->action.to) score -= 8.0;
    }
    return score;
}

static size_t choose(const Obs& obs) {
    size_t best = 0;
    double bestScore = -numeric_limits<double>::infinity();
    for (size_t i = 0; i < obs.legal.size(); ++i) {
        const Action& action = obs.legal[i];
        double score = action.type == "place" ? placementScore(obs, action) : moveScore(obs, action);
        if (score > bestScore) { bestScore = score; best = i; }
    }
    return best;
}

static string toJson(const Action& action) {
    if (action.type == "place")
        return "{\"type\":\"place\",\"piece\":\"" + action.piece + "\",\"to\":\"" + action.to + "\"}";
    string result = "{\"type\":\"move\",\"from\":\"" + action.from + "\",\"to\":\"" + action.to + "\"";
    if (!action.promotion.empty()) result += ",\"promotion\":\"" + action.promotion + "\"";
    return result + "}";
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
