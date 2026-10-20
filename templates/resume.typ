// resume.typ — one page, ATS-clean single column. Data arrives via sys.inputs.data (JSON).
#let data = json(bytes(sys.inputs.data))

#set page(paper: "us-letter", margin: (x: 0.65in, y: 0.55in))
#set text(font: "Helvetica", size: 10pt)
#set par(leading: 0.5em)

#let header = data.identity
#align(center)[
  #text(size: 17pt, weight: "bold")[#header.name]
  #v(1pt)
  #text(size: 9.5pt)[#header.display_location  |  #header.phone  |  #header.email  |  #header.linkedin]
]
#v(3pt)
#text(size: 10pt, style: "italic")[#data.positioning]
#v(2pt)
#line(length: 100%, stroke: 0.6pt)

#if data.skills.len() > 0 [
  #v(2pt)
  #text(size: 9pt)[#data.skills.join("  |  ")]
  #v(2pt)
  #line(length: 100%, stroke: 0.6pt)
]

#v(3pt)
#text(size: 11pt, weight: "bold")[PROFESSIONAL EXPERIENCE]
#v(1pt)

#for role in data.roles [
  #v(4pt)
  #grid(columns: (1fr, auto),
    [#text(weight: "bold")[#role.company] — #role.title],
    [#text(size: 9pt)[#role.dates]]
  )
  #for b in role.bullets [
    #pad(left: 8pt)[• #b]
  ]
]

// older roles with no selected bullet: one line, never an empty header
#if "earlier" in data and data.earlier.len() > 0 [
  #v(4pt)
  #text(weight: "bold")[Earlier experience:]
  #data.earlier.map(e => e.title + ", " + e.company + " (" + e.dates + ")").join("; ")
]

#v(6pt)
#line(length: 100%, stroke: 0.6pt)
#v(2pt)
#text(size: 11pt, weight: "bold")[EDUCATION]
#v(1pt)
#for ed in data.education [
  #grid(columns: (1fr, auto), [#ed.school#if ed.degree != "" [ — #ed.degree]], [#text(size: 9pt)[#ed.years]])
]
