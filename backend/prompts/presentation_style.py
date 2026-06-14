"""
Presentation style instructions passed to Gamma as additional_instructions.

Extracted verbatim from Langflow Prompt Template (Prompt Template-rwYSh).
"""

PRESENTATION_STYLE_PROMPT = """\
Create a clear, friendly, and professional customer-facing product training presentation for non-technical users.

Presentation purpose:
* The source material is internal product documentation.
* The presentation is intended for customers and end users of the platform.
* The goal is to help customers understand and use the product clearly and confidently.
* Focus on clarity, usability, workflows, and practical understanding.
* Explain concepts in simple, conversational language — avoid technical jargon and write as if explaining to a non-technical user for the first time

Content handling:
* Preserve important technical and workflow-related information.
* Do not aggressively summarize important concepts, features, workflows, or configurations.
* Present information progressively and logically.
* Break complex topics into smaller understandable sections.
* Prefer workflow-oriented explanations over abstract descriptions.

Language and formatting:
* Use formatting, alignment, and reading direction appropriate for the selected presentation language.
* The presentation language determines the layout direction for ALL slides — never switch alignment because of English technical terms.
* Maintain consistent alignment and text direction across all slides.
* Keep titles, subtitles, and bullet points consistently aligned and visually balanced.
* CRITICAL: Every sentence, title, and bullet point MUST begin with a Hebrew word. If the source content starts with an English term, restructure the sentence so it begins with Hebrew. For example, transform "Skills הם אבני בניין" into "אבני הבניין נקראים Skills".
* When the presentation language is Hebrew, all of the following must use RTL layout with right-aligned text:
  - Bullet point lists — the bullet symbol must appear on the right side
  - Numbered lists — the number must appear on the right side
  - Nested and indented lists
  - Tables — column headers and cell content must be right-aligned, reading direction right to left

Slide readability:
* Keep slides concise, visually clean, and easy to follow.
* Avoid overcrowding slides with excessive text.
* Split large topics across multiple slides when appropriate.
* Prefer short readable bullet points over dense paragraphs.
* Keep each slide focused on a single concept, feature, or workflow step.

Visual style:
* Match the visual type to the actual content of each slide.
* Use technology-oriented visuals for technical topics such as AI agents, workflows, integrations, APIs, automation, tools, and platform capabilities.
* Prefer diagrams, workflow visuals, abstract AI concepts, process illustrations, and product-oriented imagery for technical explanations.
* Do not overuse people-centric imagery across the presentation.
* Most technical slides should prioritize technology, workflow, or product-oriented visuals over human imagery.
* Maintain a consistent modern professional visual identity across the presentation.

Presentation style:
* Use a modern, premium, clean, and professional presentation style.
* Keep the presentation visually organized and easy to navigate.
* Use clear slide titles and logical section flow.
* The final presentation should feel educational, practical, approachable, and customer-friendly.

Screenshots and visual assets:
* Use provided screenshots when relevant to the slide content — workflows, UI screens, platform features, or product behavior.
* Prefer real product screenshots over generated imagery when available.
* Place screenshots near the related explanation on the most relevant slide.
* Do not force all screenshots — only use what genuinely improves understanding.
* Maintain a professional and visually balanced layout when incorporating screenshots."""
