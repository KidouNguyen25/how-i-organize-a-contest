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
// Bắt Vua > bắt quân > phong cấp > tiến lên; có nhiễu nhỏ để tránh lặp.
size_t choose(const Obs& obs) {
    size_t best = 0; double bestScore = -1e18;
    for (size_t i = 0; i < obs.legal.size(); i++) {
        const Action& a = obs.legal[i];
        double s = (rng() % 1000) / 1000.0;
        if (a.type == "move") {
            if (isCapture(obs, a)) s += obs.board.at(a.to).piece == "king" ? 10000 : 100;
            if (!a.promotion.empty()) s += 50;
            s += forward(obs, a) * 5;
            if (obs.board.at(a.from).piece == "king") s -= 20;
        }
        if (s > bestScore) { bestScore = s; best = i; }
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
