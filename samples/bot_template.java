// OTT v1 Java 17 bot. Read one observation line and return one legal action line.
// Keep stdout reserved for actions. Write diagnostics to stderr.
import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;

public class Main {
    private static final int MAX_LINE_CHARS = 1 << 20;

    private static String firstLegalAction(String observation) {
        int key = observation.indexOf("\"legal_actions\"");
        if (key < 0) return "{}";
        int start = observation.indexOf('[', key);
        if (start < 0) return "{}";
        start++;
        while (start < observation.length() && Character.isWhitespace(observation.charAt(start))) start++;
        if (start == observation.length() || observation.charAt(start) != '{') return "{}";

        int depth = 0;
        boolean inString = false;
        boolean escaped = false;
        for (int i = start; i < observation.length(); i++) {
            char value = observation.charAt(i);
            if (inString) {
                if (escaped) escaped = false;
                else if (value == '\\') escaped = true;
                else if (value == '"') inString = false;
            } else if (value == '"') inString = true;
            else if (value == '{') depth++;
            else if (value == '}' && --depth == 0) return observation.substring(start, i + 1);
        }
        return "{}";
    }

    public static void main(String[] args) throws IOException {
        try (BufferedReader input = new BufferedReader(
                new InputStreamReader(System.in, StandardCharsets.UTF_8))) {
            String observation;
            while ((observation = input.readLine()) != null) {
                String action = observation.length() <= MAX_LINE_CHARS ? firstLegalAction(observation) : "{}";
                System.out.println(action);
                System.out.flush();
            }
        }
    }
}
