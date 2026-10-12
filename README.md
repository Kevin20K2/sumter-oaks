# Sumter Oaks

Four neighbors on Magnolia Loop, run by Claude, one tick at a time.

## What's here

| File | What it is |
|---|---|
| `world.json` | The town: zones, laws, and the four residents |
| `seed.json` | The opening scene (tick 1) and each resident's starting memories |
| `prompt_template.md` | The resident prompt, filled in fresh every tick |
| `config.json` | Models, minutes per tick, day hours, memory settings |
| `sim.py` | The loop. Runs ticks and writes `data/` |
| `viewer/index.html` | Sumter Oaks Live, the page that shows the ticks |
| `data/ticks/` | One JSON file per tick (created when you run the sim) |
| `data/state.json` | Where everyone is and what they remember (created when you run the sim) |
| `data/logs/` | The exact prompt and raw reply for every resident, every tick, for debugging |

## First run on Windows

Open the `sumter-oaks` folder in File Explorer, right-click an empty spot, and choose **Open in Terminal**. Then:

```powershell
python --version                          # needs 3.9 or newer
python -m pip install -r requirements.txt
copy .env.example .env
notepad .env                              # paste your key after ANTHROPIC_API_KEY=, save, close
python sim.py --mock --ticks 3            # free: no API calls, checks everything is wired up
python sim.py --reset --test --ticks 3    # cheap: real calls, all on Haiku
```

## Watching it

```powershell
python -m http.server 8000
```

Then open http://localhost:8000/viewer/ in your browser. The page checks for new ticks every minute, so you can leave it open in one window and run ticks in another terminal.

## Everyday commands

```powershell
python sim.py                  # one tick with the real models (Sonnet residents, Haiku story)
python sim.py --ticks 6        # six ticks (three hours of town time at 30 minutes a tick)
python sim.py --test           # use Haiku for everything
python sim.py --reset          # wipe data/ and start over from seed.json
```

## How a tick works

1. Each resident's prompt is built from `world.json`, their position, what they saw last tick, and their most recent and most important memories.
2. All four residents decide at the same time.
3. The engine checks every action. A move must go to a connected zone, speech needs the listener in the same zone, objects must be in the zone, and laws and votes happen only at Town Hall. Anything else becomes WAIT, and the reason appears as `engine_note` in the tick file.
4. Positions, sightings, memories, proposals and Town Hall matters are updated. Proposals close when everyone has voted or after `proposal_open_ticks`. A majority YES adds the law.
5. The story model writes the short paragraph the viewer shows.
6. Every `reflect_every_ticks` ticks, each resident forms two new beliefs from their recent memories.
7. At 10 PM everyone goes home and the clock jumps to 7 AM.

## Cost

About $0.025 per tick with the default models, and about $0.002 per tick with `--test`. Memories grow over time, so expect that to rise a little.
