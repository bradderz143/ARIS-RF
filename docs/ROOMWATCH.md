# Room watch

A room watch is a long, unattended watch of the bands where small transmitters are commonly found. It records a
quiet reference of the room's radio surroundings, then compares the air with that reference every few minutes for
hours or days, and tells you when a carrier appears that was not there, or one that was there grows.

It is receive-only and keeps levels and frequencies, nothing else. It never listens to, records or transcribes
audio.

## What it can and cannot tell you

- It sees transmitters that are **on** in a band a receiver here **tunes**, strongly enough to stand about 10 dB
  over the reference, for long enough to be caught by a sweep. A steady carrier is found within a round or two.
- It does **not** see: bands no receiver here tunes (they are listed as "not covered", never as clear); devices
  that only record and never transmit; signals spread under the noise; a transmitter that is on for a second every
  few minutes may be missed for a long time, because each band is looked at for seconds per round.
- **Anything already transmitting while the reference is recorded counts as normal and never alerts.** The report
  lists the steady signals that were on the air at the reference and that no broadcast or phone band explains:
  Decode the ones you do not know.
- An alert means "worth a look". A new carrier is what a transmitter that was not there before looks like, and
  also what a new router, a charger or a phone looks like. A watch with no alerts means nothing obvious appeared in the bands that
  were covered. It is not proof that a room is clean.

## The bands

The watch runs without a gap from 25 MHz to 6 GHz, in these bands:

| Band | What is usually there |
| --- | --- |
| 25-88 MHz | VHF low, older wireless microphones |
| 88-108 MHz | FM broadcast band (the classic place for a simple transmitter) |
| 108-174 MHz | air band and VHF two-way radio |
| 174-230 MHz | VHF TV and DAB |
| 230-380 MHz | VHF / UHF air, 315 MHz devices |
| 380-470 MHz | UHF two-way radio, 433 MHz devices |
| 470-700 MHz | UHF TV band, wireless microphones |
| 700-960 MHz | mobile phones, trunked radio, sub-GHz devices |
| 960-1300 MHz | air navigation, radar, 1.2 GHz video links |
| 1300-2170 MHz | satellite navigation, L band, mobile phones |
| 2170-2700 MHz | 2.3 GHz, the 2.4 GHz band, 2.6 GHz mobile |
| 2700-5150 MHz | radar, 3.5 GHz mobile, C band |
| 5150-6000 MHz | 5 GHz and 6 GHz bands |

Each band is cut into sweeps of at most 200 MHz at the sweep step of the settings (10 kHz), and never across the
edge of the FM band, where a receiver's gain changes. An RTL-SDR tunes about 24-1766 MHz, so with RTL-SDR dongles
alone everything above is not covered, and the watch says so ("Not covered by any receiver here: 1766-6000 MHz")
at the start, on the card and in the report.
A receiver that tunes higher (see [DEVICES.md](DEVICES.md)) covers them. A receiver that is plugged in and added
(say "check the system") while a watch runs joins it at the next round; it records its own reference in the
following rounds while the other receivers keep sweeping, and the timeline and the report say that it was
recorded late.

Every receiver works at its own pace. One that is slow, busy or stuck never holds the others back. A receiver
that tunes far more than the others (so that its whole list does not fit into a round) sweeps the bands only it
tunes every round and takes the shared ones in turn.

## Running one

On the **Activity** tab, the Room watch card:

1. Make the room as it will be, but quiet: phones off, no equipment that will not be there later switched on.
2. Choose the length and press **Start**. Confirm. The reference takes several minutes (about half a minute per
   band, the receivers working at the same time).
3. Leave it. The card shows the round count, how long a round takes, each receiver that is not working, and
   anything that needs a look.

The reference of a watch is always its own: a reference file that an earlier watch or a rehearsal left behind is
never used. A reference sweep that could not be recorded at the start (a receiver busy for a moment) is recorded
in the first rounds, and the report lists it as recorded late. After three failed tries the watch says that this
band is not watched on that receiver for now, and goes on trying every half hour. A reference that Reset Scanner
cut short is recorded again.

In the chat: "start a room watch for 3 days", "room watch status", "room watch report", "stop the room watch",
"mark the break" (a note in the timeline), "446.1 is our radio microphone" (accept a carrier as known).

**Stopping and starting again.** A new reference takes whatever transmits at that moment as normal. So when an
earlier watch exists, the page offers two buttons, and the chat asks which you mean:

- **Carry on** keeps the reference and everything seen so far. Use it after a stop or a restart within one watch.
- **New watch** records a new reference. Use it for another room or another day's start from scratch.

A restart of ARIS or of the computer carries a running watch on by itself; the gap is written into the timeline.
Reset Scanner does not end a watch: it ends the sweeps in flight and the watch carries on with the next ones.

## Alerts

| It says | It means |
| --- | --- |
| New steady signal at X MHz ... there again now | nothing was on that frequency at the reference; a carrier was heard, and heard again at a later look |
| X MHz is N dB stronger than in the quiet reference | something was there; now there is much more (a transmitter beside or on top of an existing signal) |
| X MHz is N dB stronger on Dongle 2 than in any look up to its first alert | a signal seen earlier in this watch grew, on one receiver against its own earlier readings |
| X MHz was on in N looks over M min | in a phone, Wi-Fi or Bluetooth band a new signal is only a note at first (phones come and go); it alerts when it stays |
| X MHz has now been on in N looks since HH:MM. It comes and goes | a carrier that is on for a while every so often: three looks at least five minutes apart over half an hour or more found it, even though no two looks close together did |
| ... was first seen as bursts and is steady in this look | bursts are notes; a steady carrier is an alert |
| A bursting signal at X MHz has now been heard N times | bursts that keep coming back on one new frequency, outside the bands where sensors and phones burst |
| The signal you accepted at X MHz reads N dB stronger than when you accepted it | "This one is mine" silences a carrier as it was; a second transmitter on the same frequency, or the same one much closer, is told |
| The whole A-B MHz sweep on Dongle 1 reads N dB over its reference | a very strong transmitter is close to that antenna, or the receiver is overloaded; what it sees in that state may be its own mixing products and is only noted at first |

When only the noise of a band rises (equipment switched on nearby) and no strong signal is in sight, that is a
note, not an alert: the watch says how much less sensitive it is there, and judges new signals as before.

A new steady signal under 25 dB over the reference is a note the first time and an alert when a look within 20
minutes finds it again: a passing two-way radio call is over by then. One of 25 dB or more alerts at once. With
several receivers the next look comes within a minute or two. A wide signal (a data or video link several MHz
wide) is one alert, whatever part of it reads strongest in a look, and accepting it covers all of it.

An alert is written to the timeline, sent to the chat, and shown as a red bar on every tab with a sound and a
vibration, repeated every minute until someone presses **Seen**. On the alert's row: **Decode** (what is it),
**Locate** (roughly where, with several receivers), **This one is mine** (it stops alerting and the report lists
it as accepted by you). After six alerts in an hour the rest are logged and reported, not each sent to the chat.

Levels of different receivers are never compared with each other: each receiver is judged against its own
reference.

## When the watch itself needs a look

The watch says so, as loudly as an alert, when:

- no sweep worked in two rounds running (such a round is never counted as a round);
- one receiver had no working sweep in two rounds running: it is named, with the bands only it tunes (not
  watched meanwhile) and the receivers that still check the bands it shares; it is tried again every round and
  comes back by itself when it is plugged in again;
- one receiver has been stuck in a single sweep for ten minutes (the others carry on);
- a band could not be swept by any receiver for half an hour (a receiver busy with a capture, unplugged, failing);
- the computer slept, the program was held up, or ARIS was not running for more than ten minutes (a crash, a
  restart of the computer): the time in which nothing was watched is written into the timeline as a gap, and the
  card and the report give the total;
- a reference was replaced by a baseline recorded outside the watch, or a receiver heard none of the strong
  signals the others heard when the reference was recorded (no antenna);
- a receiver no longer hears the strong signals of its own reference (an antenna or cable came off: its sweeps
  would otherwise go on and see nothing);
- it has made no progress for a quarter of an hour, or its own thread failed (it starts itself again).

When the sweeps work again after a "no sweep worked" alarm, the watch says so (also to the alert address), so
whoever was woken can see that it is over.

Twice a day (08:00 and 20:00) it writes one line, "all is well" with the numbers. If you rely on the alert
address below, that line arriving is the sign of life.

The page itself also says when it has lost contact: after two polls without an answer from ARIS while a watch
runs, the red bar reads "No answer from ARIS since ..." and rings. "Seen" silences an alert only once ARIS has
confirmed it.

## Alerts to a phone whose screen is off

A web page cannot ring a locked phone. Two things help:

- **Keep screen on** on the card keeps the phone or tablet awake while the page is open (on a charger, next to
  you): the alarm bar then rings.
- **Alerts to a locked phone**: give the address of a push service and every alert is also posted there as a
  plain HTTP POST (title and text). A topic of the ntfy app works as it is: `https://ntfy.sh/<a-long-random-name>`
  with the same topic subscribed in the app. Unless you tick the box, only a fixed sentence is sent, so no
  frequency leaves the computer: "something needs a look" for an alert or a problem of the watch itself (high
  priority), "running, nothing needs you" for the twice-daily line, "finished" at the end. A post the service
  does not take is tried again every minute for half an hour. The address is stored in the station's config and never
  shown again in full. **Send a test** rings the phone once. This needs the computer to reach that service.

## The report

**Report** on the card (or "room watch report") gives the watch as text: what was watched with which receivers
and steps, what was not covered, what was on the air at the reference, what you accepted as known, then the
timeline per day (alerts, notes, your marks, gaps, health messages, when alerts were marked as seen), with the
references that were recorded late and the total time in which nothing was watched. The timeline's cap only ever
drops old notes: every alert, mark, gap and health message stays. It can be
downloaded as a text file. When a watch ends, the report is also saved in the data folder under `reports/` and
its SHA-256 is written into the timeline.

## Before a long watch: the computer

- Switch off sleep, suspend and the lid switch for the length of the watch. On a Linux computer with systemd:
  `sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target`, and
  `HandleLidSwitch=ignore` in `/etc/systemd/logind.conf` (undo both afterwards). A computer that sleeps watches
  nothing; ARIS notices the gap only when it wakes.
- Mains power, a powered USB hub for several receivers, and air around the computer: it sweeps for days.

## Before a long watch: a rehearsal

Run a watch the evening before, in the same place if you can, and let it run overnight in the empty room. It
alerts once for each radio channel in use around you that happened to be silent during the reference. In the
morning read the report, accept what is yours, press Seen, and **Carry on** when the room is in use: from then on
only what is new alerts. While it runs, try each of these once:

1. Switch on a transmitter you own (a handheld radio, a wireless microphone) after the reference: the alert
   should arrive within a round or two. Press Decode and Seen.
2. Make a phone call next to the antennas for a few minutes: it should be a note, and an alert only if it stays.
3. Unplug one receiver: after two rounds it should be named as not working. Plug it in again: it should come
   back by itself within a few rounds.
4. Take one antenna off for ten minutes: "no longer hears".
5. Press Reset Scanner: the watch should carry on.
6. Restart ARIS: the watch should carry on, with the gap in the timeline.
7. If you use the alert address: Send a test with the phone locked.
8. Read the Report.

## Limits to keep in mind

Every threshold here comes from simulated signals. The number of alerts a real room produces in a day, the time a
round takes on real receivers and the reach of each antenna are only known once it has run where you use it.
