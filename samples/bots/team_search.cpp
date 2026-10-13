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
        string out;
        ++pos;
        while (pos < text.size() && text[pos] != '"') {
            if (text[pos] == '\\' && pos + 1 < text.size()) {
                ++pos;
                char c = text[pos];
                if (c == 'n') out += '\n';
                else if (c == 't') out += '\t';
                else if (c == 'r') out += '\r';
                else if (c == 'u') { out += '?'; pos += 4; }
                else out += c;
            } else out += text[pos];
            ++pos;
        }
        if (pos < text.size()) ++pos;
        return out;
    }
    Json value() {
        ws();
        Json out;
        if (pos >= text.size()) return out;
        if (text[pos] == '{') {
            out.type = Json::Obj; ++pos; ws();
            while (pos < text.size() && text[pos] != '}') {
                ws(); string key = str(); ws(); ++pos;
                out.o.emplace_back(key, value()); ws();
                if (pos < text.size() && text[pos] == ',') ++pos;
                ws();
            }
            if (pos < text.size()) ++pos;
        } else if (text[pos] == '[') {
            out.type = Json::Arr; ++pos; ws();
            while (pos < text.size() && text[pos] != ']') {
                out.a.push_back(value()); ws();
                if (pos < text.size() && text[pos] == ',') ++pos;
                ws();
            }
            if (pos < text.size()) ++pos;
        } else if (text[pos] == '"') {
            out.type = Json::Str; out.s = str();
        } else if (text.compare(pos, 4, "true") == 0) {
            out.type = Json::Bool; out.b = true; pos += 4;
        } else if (text.compare(pos, 5, "false") == 0) {
            out.type = Json::Bool; pos += 5;
        } else if (text.compare(pos, 4, "null") == 0) {
            pos += 4;
        } else {
            out.type = Json::Num;
            size_t end = pos;
            while (end < text.size() && (isdigit((unsigned char)text[end]) || strchr("+-.eE", text[end]))) ++end;
            if (end > pos) out.n = stod(text.substr(pos, end - pos));
            pos = end;
        }
        return out;
    }
};

enum PieceType { EMPTY = 0, SCISSORS = 1, HAMMER = 2, PAPER = 3, KING = 4 };
enum Promotion { NONE = 0, ROOK = 1, BISHOP = 2 };
struct Piece { int side = 0, type = EMPTY, promotion = NONE; };
using Board = array<Piece, 81>;

struct Action {
    string type, pieceName, from, to, promotionName;
    int piece = EMPTY, promo = NONE;
};

int pieceType(const string& name) {
    if (name == "scissors") return SCISSORS;
    if (name == "hammer") return HAMMER;
    if (name == "paper") return PAPER;
    if (name == "king") return KING;
    return EMPTY;
}
int promoType(const string& name) {
    if (name == "rook") return ROOK;
    if (name == "bishop") return BISHOP;
    return NONE;
}
int squareIndex(const string& square) {
    if (square.size() != 2 || square[0] < 'a' || square[0] > 'i' || square[1] < '1' || square[1] > '9') return -1;
    return (square[1] - '1') * 9 + (square[0] - 'a');
}
string squareName(int index) {
    return string(1, char('a' + index % 9)) + char('1' + index / 9);
}
int fileOf(int index) { return index % 9; }
int rankOf(int index) { return index / 9; }
int sideFromName(const string& name) { return name == "white" ? 1 : 2; }
int forwardFor(int side) { return side == 1 ? 1 : -1; }

struct Observation {
    string phase, sideName;
    int side = 1;
    Board board{};
    vector<Action> legal;
};

Action parseAction(const Json& item) {
    Action action;
    action.type = item["type"].s;
    action.pieceName = item["piece"].s;
    action.piece = pieceType(action.pieceName);
    action.from = item["from"].s;
    action.to = item["to"].s;
    action.promotionName = item["promotion"].s;
    action.promo = promoType(action.promotionName);
    return action;
}

Observation parseObservation(const Json& root) {
    Observation obs;
    obs.phase = root["phase"].s;
    obs.sideName = root["side"].s;
    obs.side = sideFromName(obs.sideName);
    for (const auto& entry : root["board"].o) {
        int index = squareIndex(entry.first);
        if (index < 0) continue;
        const Json& value = entry.second;
        obs.board[index] = {sideFromName(value["side"].s), pieceType(value["piece"].s), promoType(value["promotion"].s)};
    }
    for (const Json& item : root["legal_actions"].a) obs.legal.push_back(parseAction(item));
    return obs;
}

bool beats(int attacker, int target) {
    return (attacker == SCISSORS && target == PAPER) ||
           (attacker == PAPER && target == HAMMER) ||
           (attacker == HAMMER && target == SCISSORS);
}
bool canCapture(const Piece& attacker, const Piece& target) {
    if (target.type == KING) return true;
    if (beats(attacker.type, target.type)) return true;
    return attacker.promotion != NONE && attacker.type == target.type;
}

void addMove(vector<Action>& out, int from, int to, const Piece& piece) {
    Action action;
    action.type = "move";
    action.from = squareName(from);
    action.to = squareName(to);
    if (piece.type != KING && piece.promotion == NONE && rankOf(to) == (piece.side == 1 ? 8 : 0)) {
        action.promo = ROOK; action.promotionName = "rook"; out.push_back(action);
        action.promo = BISHOP; action.promotionName = "bishop"; out.push_back(action);
    } else out.push_back(action);
}

vector<Action> generateMoves(const Board& board, int side) {
    static const int eight[8][2] = {{1,0},{-1,0},{0,1},{0,-1},{1,1},{1,-1},{-1,1},{-1,-1}};
    static const int ortho[4][2] = {{1,0},{-1,0},{0,1},{0,-1}};
    static const int diag[4][2] = {{1,1},{1,-1},{-1,1},{-1,-1}};
    vector<Action> moves;
    auto emit = [&](int from, int nf, int nr, bool mayCapture) {
        if (nf < 0 || nf >= 9 || nr < 0 || nr >= 9) return false;
        int to = nr * 9 + nf;
        const Piece& target = board[to];
        if (target.side == 0) { addMove(moves, from, to, board[from]); return true; }
        if (target.side != side && mayCapture && canCapture(board[from], target)) addMove(moves, from, to, board[from]);
        return false;
    };

    for (int from = 0; from < 81; ++from) {
        const Piece& piece = board[from];
        if (piece.side != side) continue;
        int f = fileOf(from), r = rankOf(from);
        if (piece.type == KING) {
            for (const auto& d : eight) {
                int nf = f + d[0], nr = r + d[1];
                if (nf >= 0 && nf < 9 && nr >= 0 && nr < 9 && board[nr * 9 + nf].side == 0)
                    addMove(moves, from, nr * 9 + nf, piece);
            }
        } else if (piece.promotion == NONE) {
            int fw = forwardFor(side);
            emit(from, f, r + fw, true);
            emit(from, f - 1, r + fw, true);
            emit(from, f + 1, r + fw, true);
            emit(from, f - 1, r - fw, false);
            emit(from, f + 1, r - fw, false);
        } else {
            const int (*slides)[2] = piece.promotion == ROOK ? ortho : diag;
            for (int k = 0; k < 4; ++k) {
                int nf = f + slides[k][0], nr = r + slides[k][1];
                while (emit(from, nf, nr, true)) { nf += slides[k][0]; nr += slides[k][1]; }
            }
            for (const auto& d : eight) {
                bool slidingDirection = piece.promotion == ROOK ? (d[0] == 0 || d[1] == 0) : (abs(d[0]) == abs(d[1]));
                if (!slidingDirection) emit(from, f + d[0], r + d[1], true);
            }
        }
    }
    return moves;
}

void applyMove(Board& board, const Action& action) {
    int from = squareIndex(action.from), to = squareIndex(action.to);
    if (from < 0 || to < 0) return;
    Piece moving = board[from];
    board[from] = {};
    if (action.promo != NONE) moving.promotion = action.promo;
    board[to] = moving;
}

bool canCaptureAt(const Board& board, int from, int to) {
    const Piece& attacker = board[from];
    const Piece& target = board[to];
    if (attacker.side == 0 || target.side == 0 || attacker.side == target.side || attacker.type == KING || !canCapture(attacker, target)) return false;
    int df = fileOf(to) - fileOf(from), dr = rankOf(to) - rankOf(from);
    if (attacker.promotion == NONE) {
        int fw = forwardFor(attacker.side);
        return dr == fw && abs(df) <= 1;
    }
    if (max(abs(df), abs(dr)) == 1) return true;
    bool aligned = attacker.promotion == ROOK ? (df == 0 || dr == 0) : (abs(df) == abs(dr));
    if (!aligned) return false;
    int sf = (df > 0) - (df < 0), sr = (dr > 0) - (dr < 0);
    int f = fileOf(from) + sf, r = rankOf(from) + sr;
    while (f != fileOf(to) || r != rankOf(to)) {
        if (board[r * 9 + f].side != 0) return false;
        f += sf; r += sr;
    }
    return true;
}

int kingSquare(const Board& board, int side) {
    for (int i = 0; i < 81; ++i) if (board[i].side == side && board[i].type == KING) return i;
    return -1;
}
bool kingAttacked(const Board& board, int kingSide) {
    int target = kingSquare(board, kingSide);
    if (target < 0) return false;
    for (int from = 0; from < 81; ++from)
        if (board[from].side != 0 && board[from].side != kingSide && canCaptureAt(board, from, target)) return true;
    return false;
}

int pieceValue(const Piece& piece) {
    if (piece.type == KING) return 0;
    return 100 + (piece.promotion == NONE ? 0 : 55);
}

int evaluate(const Board& board, int us, int sideToMove) {
    int them = 3 - us;
    int ownKing = kingSquare(board, us), enemyKing = kingSquare(board, them);
    if (ownKing < 0) return -1000000;
    if (enemyKing < 0) return 1000000;

    int score = 0;
    for (int i = 0; i < 81; ++i) {
        const Piece& piece = board[i];
        if (piece.side == 0) continue;
        int sign = piece.side == us ? 1 : -1;
        if (piece.type != KING) {
            score += sign * pieceValue(piece);
            int progress = piece.side == 1 ? rankOf(i) : 8 - rankOf(i);
            score += sign * (progress * (piece.promotion == NONE ? 3 : 1));
            if (piece.promotion != NONE) score += sign * 12;
        }
    }

    bool ownKingThreat = kingAttacked(board, us);
    bool enemyKingThreat = kingAttacked(board, them);
    if (ownKingThreat) score -= (sideToMove == them ? 18000 : 750);
    if (enemyKingThreat) score += (sideToMove == us ? 18000 : 750);

    for (int target = 0; target < 81; ++target) {
        const Piece& victim = board[target];
        if (victim.side == 0 || victim.type == KING) continue;
        bool attacked = false;
        for (int from = 0; from < 81 && !attacked; ++from)
            if (board[from].side != 0 && board[from].side != victim.side && canCaptureAt(board, from, target)) attacked = true;
        if (attacked) {
            int sign = victim.side == us ? -1 : 1;
            int penalty = sideToMove != victim.side ? pieceValue(victim) * 45 / 100 : pieceValue(victim) * 12 / 100;
            score += sign * penalty;
        }
    }

    return score;
}

struct RankedMove { Action action; int score; int originalIndex = -1; };

int search(const Observation& obs) {
    const int us = obs.side, them = 3 - us;
    vector<RankedMove> roots;
    roots.reserve(obs.legal.size());
    for (int i = 0; i < (int)obs.legal.size(); ++i) {
        const Action& action = obs.legal[i];
        Board next = obs.board;
        applyMove(next, action);
        roots.push_back({action, evaluate(next, us, them), i});
    }
    stable_sort(roots.begin(), roots.end(), [](const RankedMove& a, const RankedMove& b) { return a.score > b.score; });
    if (roots.size() > 22) roots.resize(22);

    int bestIndex = 0;
    int bestValue = numeric_limits<int>::min();
    for (int ri = 0; ri < (int)roots.size(); ++ri) {
        Board afterUs = obs.board;
        applyMove(afterUs, roots[ri].action);
        if (kingSquare(afterUs, them) < 0) return roots[ri].originalIndex;

        vector<RankedMove> replies;
        for (const Action& response : generateMoves(afterUs, them)) {
            Board afterThem = afterUs;
            applyMove(afterThem, response);
            replies.push_back({response, evaluate(afterThem, us, us)});
        }
        if (replies.empty()) {
            if (1000000 > bestValue) { bestValue = 1000000; bestIndex = ri; }
            continue;
        }
        stable_sort(replies.begin(), replies.end(), [](const RankedMove& a, const RankedMove& b) { return a.score < b.score; });
        if (replies.size() > 10) replies.resize(10);

        int worstCase = numeric_limits<int>::max();
        for (const RankedMove& response : replies) {
            Board afterThem = afterUs;
            applyMove(afterThem, response.action);
            if (kingSquare(afterThem, us) < 0) { worstCase = -1000000; break; }

            vector<Action> ownMoves = generateMoves(afterThem, us);
            if (ownMoves.empty()) { worstCase = min(worstCase, -1000000); continue; }
            int bestReply = numeric_limits<int>::min();
            for (const Action& ownReply : ownMoves) {
                Board leaf = afterThem;
                applyMove(leaf, ownReply);
                bestReply = max(bestReply, evaluate(leaf, us, them));
                if (bestReply >= 1000000) break;
            }
            worstCase = min(worstCase, bestReply);
        }
        if (worstCase > bestValue || (worstCase == bestValue && roots[ri].score > roots[bestIndex].score)) {
            bestValue = worstCase;
            bestIndex = ri;
        }
    }
    return roots[bestIndex].originalIndex;
}

int plannedPlacement(int side, const Action& action) {
    int index = squareIndex(action.to);
    if (index < 0) return 0;
    int f = fileOf(index), r = rankOf(index);
    int homeBack = side == 1 ? 0 : 8;
    int homeFront = side == 1 ? 1 : 7;
    if (action.piece == KING) return (r == homeBack && f == 4) ? 10000 : -10000;
    if (r == homeFront) {
        static const int frontType[9] = {SCISSORS, EMPTY, HAMMER, EMPTY, PAPER, EMPTY, SCISSORS, EMPTY, HAMMER};
        if (frontType[f] == action.piece) return 1000;
    }
    if (r == homeBack) {
        static const int backType[9] = {EMPTY, PAPER, EMPTY, SCISSORS, EMPTY, HAMMER, EMPTY, PAPER, EMPTY};
        if (backType[f] == action.piece) return 900;
    }
    return 0;
}

string actionJson(const Action& action) {
    if (action.type == "place")
        return "{\"type\":\"place\",\"piece\":\"" + action.pieceName + "\",\"to\":\"" + action.to + "\"}";
    string out = "{\"type\":\"move\",\"from\":\"" + action.from + "\",\"to\":\"" + action.to + "\"";
    if (!action.promotionName.empty()) out += ",\"promotion\":\"" + action.promotionName + "\"";
    return out + "}";
}

int choose(const Observation& obs) {
    if (obs.legal.empty()) return 0;
    if (obs.phase == "placement") {
        int best = 0, bestScore = numeric_limits<int>::min();
        for (int i = 0; i < (int)obs.legal.size(); ++i) {
            int score = plannedPlacement(obs.side, obs.legal[i]);
            if (score > bestScore) { bestScore = score; best = i; }
        }
        return best;
    }
    return search(obs);
}

int main() {
    ios::sync_with_stdio(false);
    string line;
    while (getline(cin, line)) {
        if (line.empty()) continue;
        Parser parser(line);
        Observation obs = parseObservation(parser.value());
        if (obs.legal.empty()) break;
        cout << actionJson(obs.legal[choose(obs)]) << '\n' << flush;
    }
    return 0;
}
