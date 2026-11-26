# The family demo

A five-minute scene for a room full of people. Every line below was run end to end in the conversation lab
(`python -m testing.conversation_lab family barge`): three different voices on one microphone, BITCH's own voice
echoing back into the mic, the real model, and the real voice. In the last full run every reply was right, every line
meant for someone else got silence, and BITCH spoke about 2.5 seconds after each person finished.

## Before everyone comes in (once)

1. Turn on the JBL and let it connect to the Pi.
2. Update the Pi (it picks up voice streaming):

   ```bash
   bash ~/BITCH-Robot/update.sh
   ```

3. Quit and reopen the BITCH app on the Mac.
4. Tell BITCH where home is, so the weather is yours: **"Hey BITCH, I live in _your city_."**
5. Warm it up with one throwaway line: **"Hey BITCH, you awake?"** The first turn after a restart can take a few
   seconds while the models load; after that it's quick.
6. Point the Mac camera at the room. With two or more faces in view, BITCH also judges lines that don't use its name.

## The scene

| Who | Says | What BITCH does |
|---|---|---|
| You | "Hey BITCH, this is my family. Say hi to everyone." | Says hi to the room. |
| Mom | "Hi BITCH! It's nice to meet you." | Answers her. No name needed: it's still listening after its last reply. |
| You | "Mom, do you want some tea?" | **Stays quiet.** It was said to Mom. |
| Mom | "Sure, thanks honey." | **Stays quiet.** |
| Sister | "Hey BITCH, set a timer for five minutes for the tea." | Sets a real timer, and says so when it rings. |
| You | "Hey BITCH, what's the weather going to be like tomorrow?" | Real forecast for home (Open-Meteo), about 2.5 s. |
| Sister | "Hey BITCH, tell us a really short joke." | A joke. |
| Mom | "Okay, it's your turn to do the dishes." | **Stays quiet.** Family business. |
| You | "Hey BITCH, tell me a story about a robot who learns to cook." | Starts the story. |
| You (about 3 s in, over it) | "Wait, actually, make it about a robot who learns to paint." | Stops within half a second and starts the painting story. |
| You | "Thanks BITCH, that's all for now." | Signs off. |
| (five minutes later) | | The tea timer rings. |

## Tips

- One person at a time. When starting something new, or after a long pause, say "Hey BITCH" first.
- BITCH keeps listening for about 24 seconds after it answers. Anything said in that window without its name gets
  judged: lines aimed at a person ("Mom, ...", "..., right honey?") are never for BITCH. With the camera on and
  others in view, the model decides the rest.
- Other things it can really do: "Add milk to the shopping list", "What's on the list?", "How long is left on the
  timer?", "Pause the music", "You're too loud".

## Not part of the proven scene

These work in the code but weren't part of the lab run, so leave them out of a first showing:
- Recognizing each family member by face or voice. They each need to be learned first ("Hey BITCH, remember my face").
- Anything that goes through ChatGPT on the Pi (news, calendar): about 10–15 s, fine but slower.
