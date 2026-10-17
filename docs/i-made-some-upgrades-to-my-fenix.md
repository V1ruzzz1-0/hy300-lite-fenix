# I Made Some Upgrades to My fenix #
User: v1ruzzz1_0
Date: 10/17/2026
Status: Completed

While I was thinking about how to move forward with the project, I decided to make some modifications to my hy300-lite.

I decided to document these changes separately from the software project, since they aren't necessary for any of the current (or future) software projects for this projector.

WARNING: These modifications are personal, and again, they won't affect the software projects. And this is NOT a tutorial! There's no guarantee that any modification made to my projector will work the way it should on another one, so any damage caused by trying to replicate my changes is YOUR responsibility. These are just my personal analyses, not something universal.

# Remote Control

## 1. Signal Strength

The original remote was pretty bad: for it to work normally I had to be within roughly 20 centimeters of the projector (which is horrible for a projector that, in theory, should sit somewhere spacious and high for the best experience). I tried to check whether the problem was the projector's receiver, but it wasn't. In fact, it was better designed than I expected (there were 2 receivers, one near the rear ports and another near the lens). I also checked the batteries, and they were 100% fine and charged.

So I modified how the infrared LED is driven using a PNP transistor, keeping the remote's original circuit responsible for the pulse pattern, but changing how the LED gets its power: instead of going through the IC, it now comes straight from the batteries, drastically improving the LED's strength without compromising functionality.

The range improved drastically. I can now use it from a much greater distance (basically on par with quality remotes), and I don't even need to point it directly at where the receiver sits inside.

## 2. Tactile Feel

I also replaced the physical buttons (not the internal touch membranes, but the silicone membrane with the icons) with a set that feels nicer to the touch, taken from a generic remote from another projector (which, coincidentally, was identical in shape). The buttons got softer, with only some of the icons changing (which ended up a bit confusing), but it's much more comfortable.

In addition, I stacked aluminum membranes from other remotes (the ones responsible for shorting the signals so the buttons work, and for the click sound) to make the buttons firmer and the click more satisfying (although it got louder).

# Power Supply

## 1. Capacitor

One of the main things I noticed about this projector's power supply is that its transformer gets way too hot (to the point where you can't keep a finger on it for more than 5 seconds). So I decided to try a few things to fix it.

I initially considered replacing the whole power supply with one from an original Magcubic projector (I realized today that I've always said Magcubic wrong; I used to say Magicubic), which, by my analysis, would be a fully compatible transplant and would solve my problems more easily.

As an initial test, I replaced the original 1000 uF, 10V electrolytic capacitor on the main board's 5V line with a 563uF, 6.3V solid polymer capacitor.

That change didn't solve the heating problem I was investigating. However, I had the impression that the system became a bit more stable and froze less. I don't know if it's just an impression or if something really changed, but either way, it was an upgrade.

## 2. Inductor

As a second attempt (I had already lost hope that it could be related to the 5V/3A area, and thought it was instead the 27V/1.1A one), I replaced the original toroidal inductor with a spool-shaped ferrite inductor, with thicker wire and fewer turns (just 6).

One of the first things that caught my attention was that the transformer became practically cold, as if it were completely off, which was EXACTLY what I wanted from the start.

My theory is that the old inductor may not have had enough capacity to smooth current spikes in a way that was properly sized for that power supply.

I can't confirm the theory because I don't have an LCR meter, but since there was a real improvement, the swap is still worthwhile, even though the reason isn't confirmed yet.

# Backlight LED

## 1. LED Module

Originally, this projector's brightness and colors left a lot to be desired, and on load peaks the luminance varied noticeably, so I decided to make some improvements here.

The fenix's original LED module had a 10 × 6 array, totaling 60 small emitters.

I replaced that module with another taken from an old original Magcubic HY300 projector, which had a 10 × 8 array, totaling 80 emitters. The modules seemed to have similar specs. What really differed was the LED array; aside from that, the voltage and current were the same.

The most noticeable change was in the colors projected on the wall. They became much more vivid and intense than before (still within the standards it's able to deliver), although the luminance didn't change noticeably, and the module's heating became much milder than before. That suggests that even though the current module has more LEDs, it can still be more efficient, even though I haven't tested the efficiency of the two against each other.

## 2. Inductor on the LEDs

Lastly, I added another spool-shaped inductor in series with the positive wire that feeds the LED module.

From what I observed, the LED supply circuit already had capacitors and a high-voltage rectifier diode, but no inductor on that line. I didn't touch the circuit itself, other than adding the inductor in series on the wire going to the LED module, because there wasn't room to solder it onto the power supply board.

After the change, the variation dropped a lot. My rough visual estimate is that it improved around 75% of the variations overall, and it now happens much less often.

# Final

After these modifications, some differences became quite noticeable on my fenix.

The power supply's transformer no longer runs extremely hot, the projected colors became more vivid than before, and the LED variation dropped a lot.

I took advantage of the time I was writing this document to leave the fenix on and see if any instability would happen, and none did.

Overall, the changes had positive results on my fenix, which is exactly why I decided to keep them.

The project remains focused on investigating the fenix's digital system. These changes are part of my device's physical history, but they aren't requirements for investigating the firmware or developing software for this platform.

For now, I'm happy with the results. I managed to improve a few things that bothered me and, at the same time, record what I did in case someone wants to replicate it in their own way.

# Bonus

One of the first modifications I made to this projector was cutting the housing (I ended up cutting too much, but it's still fine) to glue an ESP32-C3 inside it and leave the USB port on the outside, so that whenever I connect this USB to the PC, I get access to my projector's UART.

It was a small modification, but a relevant one for the project.

/ -- /
