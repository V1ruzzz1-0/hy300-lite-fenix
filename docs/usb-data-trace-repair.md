# USB Data Trace Repair #
User: v1ruzzz1_0
Date: 10/12/2026

# Context

Earlier, I had decided not to repair the fenix's USB-A port until I got proper tools to work on the board's SMD solder points.

Since the data traces (DP and DM) were broken, my investigations would be more limited, so I decided to take the risk of attempting a repair without the proper tools.

# Procedure

For this process, I used wire from a phone speaker's voice coil to rebuild the connection up to the resistors (there are 2, one for each trace, and judging by the circuit they're probably pull-up resistors). The work was quite hard, mainly because my soldering iron's tip is about twice the size of the pads I needed to jumper, and the tip was already badly corroded.

Even with those limitations, after a lot of effort, I managed to redo the connection for both DP and DM.

I didn't have any UV resin to finish the repair, so I improvised by filling the worked area with B-7000 glue (the cheap kind used to glue on phone covers). I still think T7000 would have been more suitable, but since this was an improvisation anyway and I didn't have any, it's all good.

# Result

I managed to rebuild both of the damaged connections, and after a sanity check of the port, it's working normally without any major problems.

This repair will also let me resume my earlier investigations related to RockUSB and, above all, UMS (USB Mass Storage Gadget), which were limited by the damage to the USB port.

/ -- /
