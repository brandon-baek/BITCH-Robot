"""Read speakable phrases from the partial JSON of a local model reply."""
import json
import re

SPEECH_FIELD = re.compile(r'(?:\{|,)\s*"say"\s*:\s*"')
JSON_TEXT = re.compile(r'(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*')
SENTENCE = re.compile(r'[.!?]+["”’)]*(?=\s)')
CLAUSE = re.compile(r'[,;:]|[—–]')
ABBREVIATION = re.compile(r'\b(?:mr|mrs|ms|dr|st|vs|etc|e\.g|i\.e|[a-z])\.$', re.I)
STAGE = re.compile(r'<think>.*?</think>|\*[^*\n]+\*|\[[^]\n]+\]', re.S)
BEAT = re.compile(r'^\s*\[([nwugepsalr])\]\s*')


def clean(text):
    return ' '.join(STAGE.sub('', text).split()).strip(' "`')


class Phrases:
    def __init__(self, request):
        self.request = request.casefold().rstrip('?.!')
        self.offset = 0
        self.styles = []

    def feed(self, content):
        self.styles = []
        field = SPEECH_FIELD.search(content)
        if not field:
            return (), {}
        try:
            metadata = json.loads((content[:field.start()].strip() or '{') + '}')
            match = JSON_TEXT.match(content, field.end())
            text = json.loads('"' + match.group() + '"')
        except (ValueError, UnicodeError):
            return (), {}
        if any(0xd800 <= ord(char) <= 0xdfff for char in text):
            return (), {}
        complete = content[match.end():match.end()+1] == '"'
        text = text[:560]
        phrases = []
        while self.offset < len(text):
            tail = text[self.offset:]
            # Wait for whitespace after punctuation to avoid cutting a number or abbreviation.
            end = next((m.end() for m in SENTENCE.finditer(tail)
                        if m.end() >= 10 and not ABBREVIATION.search(tail[:m.end()])), 0)
            if not end and len(tail) >= 62:
                end = next((m.end() for m in CLAUSE.finditer(tail)
                            if 44 <= m.end() <= 90 and tail[m.end():m.end()+1] == ' '), 0)
            if not end and complete:
                end = len(tail)
            if not end:
                break
            segment = tail[:end]
            beat = BEAT.match(segment)
            phrase = clean(segment)
            # Let the completed-response path handle model output that parrots a request.
            if not self.offset and len(self.request.split()) >= 4 and (
                    self.request == phrase.casefold().rstrip('?.!') or
                    self.request.startswith(phrase.casefold().rstrip('?.!') + ' ')):
                break
            self.offset += end
            if phrase:
                phrases.append(phrase)
                self.styles.append(beat[1] if beat else None)
        return phrases, metadata
