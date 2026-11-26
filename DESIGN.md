# Design

The companion app (`app/`) uses Apple's app grammar in Brandon's palette. One screen of essentials; everything else is in a Settings sheet.

## Palette

Tokens live on `:root` in `app/style.css` and switch with `prefers-color-scheme`.

| Token | Light | Dark | Job |
|---|---|---|---|
| `--ground` | `#ECEAE6` desk grey | `#121417` | Page, sheet, top bar glass |
| `--surface` | `#FBFAF8` | `#1C1F24` | Grouped lists, BITCH's bubbles, inputs |
| `--ink` / `--ink-2` / `--ink-3` | `#18202E` ink navy, `#565F6F`, `#6E7685` | `#ECEEF1`, `#A9AFB9`, `#8C929D` | Text, secondary text, placeholders |
| `--user` | `#1E2A40` | `#33466B` | Your bubbles, Send, primary buttons |
| `--tint` | `#22365C` | `#A9BDE4` | Icon buttons, text buttons, links |
| `--kraft-bright` / `--kraft` | `#B98A55` / `#8F6337` | `#D6B084` | BITCH's colour: listening dot, switches, focus ring, caret, cardboard eyelids |
| `--danger` | `#B3402C` | `#F0826D` | Stop, muted mic, errors |

Kraft belongs to BITCH. Navy belongs to you. Red only means something is off or stopping.

## Type

- Large titles: Helvetica Neue Bold, 34px (main) and 28px (sheet, pairing), tracking -0.02em.
- Everything else: the system UI font (SF on Apple devices) at Apple's sizes: 17px body and rows, 15px status and secondary, 13px section headers and footnotes.

## Layout

- Main screen is a stage, not a chat: glass top bar (name, voice mute, Settings); BITCH's eyes large in the centre with the status word (22px semibold) and who it is with ("With Brandon" plus a kraft Voice or Face badge, "Brandon is here", or "Someone new" with Add); then captions of the exchange happening now: what you said (17px, quoted, secondary) and BITCH's reply (24px). Captions fade 45 seconds after the conversation ends. "Earlier" opens the last 40 lines in a sheet. Fixed glass composer (mic mute, message field, Send). Stop floats above the composer only while BITCH thinks, talks or performs.
- Adding a person is its own sheet: name, then a round live camera view with six progress dots, then three sentences to read that tick off as each voice sample lands, then a kraft check.
- Column max 680px, centred on a wide Mac window.
- Settings: a `<dialog>` sheet. Bottom sheet on a phone, centred panel from 720px. Grouped inset lists with 0.5px hairlines inset 16px, uppercase 13px section headers, footnotes under groups.
- Controls are at least 44px. Respects safe areas.

## Signature

BITCH's googly eyes. Each pupil is a spring-and-mass that overshoots and bounces off the rim. They follow the pointer when idle, look at you while listening (pupils widen with your voice), glance up and right while thinking, and jiggle with BITCH's speech energy while talking. Cardboard (kraft) lids blink, droop halfway with your mic off, and close when BITCH is unreachable. Reduced motion places the pupils without physics and stops blinking.

## Motion

Exponential ease-out (`cubic-bezier(.22, 1, .36, 1)`). Only the eyes move continuously; the sheet rises once, Stop rises once, switches slide. No entrance animation on messages.
