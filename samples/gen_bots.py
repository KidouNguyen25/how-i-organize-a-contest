"""Generate the sample bots in samples/bots/ from bot_template.cpp by swapping the strategy block."""

from pathlib import Path

HERE = Path(__file__).resolve().parent
BEGIN = "// ---------- YOUR STRATEGY BEGIN ----------"
END = "// ---------- YOUR STRATEGY END ----------"

STRATEGIES = {
    "random": """size_t choose(const Obs& obs) {
    return rng() % obs.legal.size();
}""",
    "greedy": """// Bắt Vua > bắt quân > phong cấp > tiến lên; có nhiễu nhỏ để tránh lặp.
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
}""",
    "rusher": """// Xếp quân ở hàng trước; khi chơi luôn đẩy quân xa nhất về phía trước để phong cấp.
size_t choose(const Obs& obs) {
    size_t best = 0; double bestScore = -1e18;
    for (size_t i = 0; i < obs.legal.size(); i++) {
        const Action& a = obs.legal[i];
        double s = (rng() % 1000) / 1000.0;
        if (a.type == "place") {
            int front = obs.side == "white" ? rankOf(a.to) : 8 - rankOf(a.to);
            s += front * 10 + (a.piece == "king" ? -100 : 0);
        } else {
            int adv = obs.side == "white" ? rankOf(a.to) : 8 - rankOf(a.to);
            s += adv * 10 + forward(obs, a) * 20;
            if (isCapture(obs, a)) s += 40;
            if (!a.promotion.empty()) s += 200;
            if (obs.board.at(a.from).piece == "king") s -= 500;
        }
        if (s > bestScore) { bestScore = s; best = i; }
    }
    return best;
}""",
    "hunter": """// Săn Vua đối phương: bắt Vua ưu tiên tuyệt đối, còn lại thu hẹp khoảng cách tới Vua.
size_t choose(const Obs& obs) {
    string kingSq;
    for (auto& p : obs.board) if (p.second.piece == "king" && p.second.side != obs.side) kingSq = p.first;
    size_t best = 0; double bestScore = -1e18;
    for (size_t i = 0; i < obs.legal.size(); i++) {
        const Action& a = obs.legal[i];
        double s = (rng() % 1000) / 1000.0;
        if (a.type == "move") {
            if (isCapture(obs, a)) s += obs.board.at(a.to).piece == "king" ? 10000 : 60;
            if (!kingSq.empty()) {
                int d = max(abs(fileOf(a.to) - fileOf(kingSq)), abs(rankOf(a.to) - rankOf(kingSq)));
                s += (9 - d) * 8;
            }
            if (!a.promotion.empty()) s += 30;
            if (obs.board.at(a.from).piece == "king") s -= 200;
        }
        if (s > bestScore) { bestScore = s; best = i; }
    }
    return best;
}""",
}

if __name__ == "__main__":
    text = (HERE / "bot_template.cpp").read_text(encoding="utf-8")
    head, rest = text.split(BEGIN)
    _, tail = rest.split(END)
    out = HERE / "bots"
    out.mkdir(exist_ok=True)
    for name, body in STRATEGIES.items():
        (out / f"{name}.cpp").write_text(f"{head}{BEGIN}\n{body}\n{END}{tail}", encoding="utf-8", newline="\n")
        print("wrote", out / f"{name}.cpp")
