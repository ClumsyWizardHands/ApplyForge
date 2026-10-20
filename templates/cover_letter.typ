// cover_letter.typ — data via sys.inputs.data (JSON): identity, company, title, date, body
#let data = json(bytes(sys.inputs.data))

#set page(paper: "us-letter", margin: (x: 1in, y: 0.9in))
#set text(font: "Helvetica", size: 10.5pt)
#set par(leading: 0.62em, justify: false)

#text(size: 14pt, weight: "bold")[#data.identity.name]
#linebreak()
#text(size: 9.5pt)[#data.identity.display_location | #data.identity.phone | #data.identity.email]
#v(10pt)
#data.date
#v(6pt)
Re: #data.title — #data.company
#v(10pt)

#for para in data.body.split("\n\n") [
  #para
  #v(7pt)
]

#v(4pt)
#data.identity.name
