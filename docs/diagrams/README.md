# Figures

The diagrams in the documentation are generated from source in this directory,
not drawn by hand. Change the source, re-run, and commit both the source and
the generated SVGs.

| Figure | Claim it makes | Source |
| --- | --- | --- |
| `evidence-chain` | Every value stays tethered to the pixels it came from, and abstaining leaves nothing to tether | `make_diagrams.py` |
| `domain-layers` | A new document type is four files you write. The engine is the same code either way | `make_diagrams.py` |
| `retrieval-modes` | The question decides the machinery. Money is added up in Python, never by a language model | `make_diagrams.py` |
| `architecture` | The whole application is one container, talking to exactly three things outside itself | `make_diagrams.py` |
| `workflow` | The five steps from a PDF to a cited answer | `make_diagrams.py` |
| `scale` | The same per-field walk runs whether there is one chain or six, and a real chain caught a bug the synthetic one never could | `make_diagrams.py` |
| `supersedence.gif` | "Current" is resolved per field, by walking backwards until a document sets that field | `supersedence_anim.py` |

## Regenerating

The static figures need nothing beyond the project's own dependencies:

```bash
python docs/diagrams/make_diagrams.py
```

Each figure is written twice, `-light.svg` and `-dark.svg`, with identical
geometry and a different palette. Markdown embeds them so a reader gets the
one that matches their theme:

```html
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="diagrams/evidence-chain-dark.svg">
  <img alt="..." src="diagrams/evidence-chain-light.svg">
</picture>
```

The animation is separate, because Manim is a large dependency for one file
and nobody reading the documentation needs it installed:

```bash
pip install manim                      # plus ffmpeg on PATH
python -m manim render -qh docs/diagrams/supersedence_anim.py Supersedence
ffmpeg -i <the rendered mp4> -vf "fps=13,scale=1000:-1:flags=lanczos,palettegen=max_colors=64:stats_mode=diff" palette.png
ffmpeg -i <the rendered mp4> -i palette.png \
  -lavfi "fps=13,scale=1000:-1:flags=lanczos [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle" \
  -loop 0 docs/diagrams/supersedence.gif
```

The two-pass palette is not optional if you care about the file size. A naive
conversion of the same clip is several times larger for no visible gain.

## Rules these figures follow

They are explanatory graphics, not system documentation. Prose carries the
detail. A figure carries one idea.

- **One claim per figure, stated as a sentence before anything is drawn.** If
  you cannot say it in a sentence, the figure is not ready. Every claim is in
  a comment above its function.
- **Geometry specific to the argument.** Time as position, transformation as
  one artifact changing, anchoring as a physical tether. A figure that could
  describe an unrelated system after relabelling has failed and needs
  redesigning, not retouching.
- **Different arguments get different visual grammars.** The figures do not
  share a template. `retrieval-modes` draws its two branches in visibly
  different styles because the whole point is that they are different kinds of
  machinery. `workflow` stays deliberately simple because it is an orientation
  map, not a system diagram.
- **Arrows only where direction matters.** The evidence threads have no
  arrowheads, because clicking the value opens the page and clicking the page
  finds the value. A head would be a lie in one direction.
- **Empty space is allowed and stays empty.**
- **Colours track the application.** The palette comes from
  `web/src/styles.css`, so amber means evidence highlight in a figure for the
  same reason it does in the PDF viewer.
- **System font stacks only.** An SVG embedded in Markdown renders with the
  reader's fonts. Anything else silently falls back on somebody else's
  machine.

## Diagrams that are not here

Most structural diagrams, a pipeline stage list, a sequence of API calls, live
inline in the documentation as Mermaid, which GitHub renders natively and
which stays readable in a diff. [Architecture](../architecture.md) has the
full set, including the request sequence and the document-arrival flow that
this figure deliberately leaves out.

`architecture` and `workflow` earn their place here because they are the two
figures a new reader is most likely to revisit. Both use a small hand-drawn
SVG icon vocabulary instead of an icon font for generic roles, which keeps
them readable in GitHub's light and dark themes. `architecture` departs from
that vocabulary in exactly three spots, where a box names a specific product
rather than a role: the container runtime, the model endpoint, and the
database service each get that product's own real logo instead of an
invented stand-in, see `brand_logo` in `make_diagrams.py`. Use Mermaid for
ordinary maps of components or API sequences, and use this directory for a
figure that carries a specific visual argument.
