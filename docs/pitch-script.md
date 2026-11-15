# Pitch script: AstroSearchX (Space Apps Seattle, "240 Seconds of Glory" format)

Deck: https://claude.ai/artifact/RXXRta1rueUtkzRBZrf5Ki (6 slides; each slide's speaker notes hold its lines).

| Part | Slides | 4-min final | 3-min round |
|---|---|---|---|
| Person + Problem | 1 cover, 2 who it's for | 35 s | 35 s |
| Solution + Proof | 3 live demo, 4 crawler | 150 s (demo 90, crawler 60) | 90 s (demo 55, crawler 35) |
| What could change | 5 real-time images | 40 s | 40 s |
| What to remember | 6 close | 15 s | 15 s |

Cuts for the 3-minute round are marked ✂.

## Before you go on stage
- [ ] Pre-warm Barnard's Star in the app (run "Find what moves" once). A cold run takes 1–2 minutes.
- [ ] Have a backup screen recording of the demo in case IRSA is slow.
- [ ] Refresh the "45,000+ objects" number on slide 4 if the crawler has gone further.

---

## 1. Person + Problem (0:00–0:35)

**Slide 1, cover**
> Have you ever wondered what's out there moving in the night sky that nobody has spotted yet?

**Slide 2, who it's for**
> Our tool is for two kinds of people. People who are curious about the sky, who want to pick a spot and actually
> watch it change. And researchers, who want to use real data to find new moving objects. Here's the problem:
> NASA's SPHEREx telescope photographs the whole sky over and over, but nobody is searching those images for things
> that move. That's the gap we fill.

## 2. Solution + Proof (0:35–3:05)

**Slide 3, live demo (0:35–2:05)**
> So we built AstroSearchX. You pick any spot in the sky, by name or by coordinates. We pull NASA's real SPHEREx
> images of that spot and play them as a time-lapse. Let me show you.

*Switch to the app:*
1. Search **Barnard's Star**.
   > Each frame is a real SPHEREx image, taken months apart. Watch this star step north.
2. Click **Find what moves**.
   > The app finds it on its own and draws the path it measured in blue, right next to where other telescopes say it
   > should be, in red. They line up.
3. Click the star to open its evidence. ✂ *skip in the 3-minute round*
   > And you don't have to trust us. You can see the actual pictures, from 25 years ago until now.

> That's one star someone chose to look at. But what about the rest of the sky?

**Slide 4, crawler as proof (2:05–3:05)**
> Nobody should have to know where to look. So we built a crawler that searches the sky on its own. It starts where
> SPHEREx looks most often and spirals outward, one patch at a time. Every patch goes through the same search you
> just saw. Anything that moved gets checked against older telescopes, so false alarms get thrown out before a
> person ever sees them. What's left goes on a ranked list of candidates. It's already checked more than 45,000
> objects, and it keeps going: if it stops, it picks up right where it left off.

✂ 3-min: drop "Every patch goes through the same search you just saw" and the last sentence's second half.
*Optional: show the website's Candidates panel here.*

## 3. What could change (3:05–3:45)

**Slide 5**
> What could change? Right now we search SPHEREx images that come out months apart. Next, we want to connect it to
> real-time images straight from telescopes, taken hours or days apart instead of months. That means new objects get
> flagged as soon as the pictures come down. Shorter gaps mean we can catch fast things like asteroids and comets,
> not just slow-moving stars. And the same search can plug into other telescopes, so more of the sky gets watched,
> more often.

## 4. What to remember (3:45–4:00)

**Slide 6**
> If you remember one thing: finding new objects in the sky usually means years of searching. Our tool does that
> search for you, with real NASA data, so discovery can happen faster. We're Mongoose in Space. Thank you.

---

## If judges ask (finals have 1 minute of Q&A)
- **"Have you found anything new?"** We have a ranked list of candidates. Each still needs checking before anyone
  calls it new, and the tool says "candidate", never "discovery".
- **"How do you know it works?"** It finds known moving stars and matches other telescopes: Barnard's Star at
  10.45″/yr vs Gaia's 10.39. On stars that Gaia says stand still, it called 0 of 5,701 movers.
- **"What's the data?"** SPHEREx Data Release 1 and Quick Release 3 images from NASA's IRSA archive, checked
  against Gaia, 2MASS and WISE.
- **"What can't it see yet?"** Very slow or very faint movers, because SPHEREx images are coarse and most spots
  only get a few visits. That's why shorter gaps between images (slide 5) matter.
