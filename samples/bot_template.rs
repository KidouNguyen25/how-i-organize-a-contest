// OTT v1, Rust 2021, standard library only.
// Replace choose() for your strategy. Keep stdout reserved for actions.
use std::io::{self, BufRead, Read, Write};

fn choose(json: &str) -> Option<&str> {
    let key = "\"legal_actions\"";
    let tail = &json[json.find(key)? + key.len()..];
    let actions = tail[tail.find('[')? + 1..].trim_start();
    if !actions.starts_with('{') { return None; }
    let (mut depth, mut quoted, mut escaped) = (0, false, false);
    for (index, byte) in actions.bytes().enumerate() {
        if quoted {
            if escaped { escaped = false; }
            else if byte == b'\\' { escaped = true; }
            else if byte == b'"' { quoted = false; }
        } else {
            match byte {
                b'"' => quoted = true,
                b'{' => depth += 1,
                b'}' => {
                    depth -= 1;
                    if depth == 0 { return Some(&actions[..=index]); }
                }
                _ => {}
            }
        }
    }
    None
}

fn main() -> io::Result<()> {
    let input = io::stdin();
    let mut reader = input.lock();
    let output = io::stdout();
    let mut writer = output.lock();
    loop {
        let mut line = String::new();
        let count = (&mut reader).take(1_048_578).read_line(&mut line)?;
        if count == 0 { break; }
        if count > 1_048_576 || !line.ends_with('\n') {
            return Err(io::Error::new(io::ErrorKind::InvalidData, "Observation exceeds line limit"));
        }
        let action = choose(&line).ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "No legal action"))?;
        writeln!(writer, "{}", action)?;
        writer.flush()?;
    }
    Ok(())
}
