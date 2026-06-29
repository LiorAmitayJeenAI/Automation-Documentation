from backend.prompts.presentation_style import get_audience_framing

_LANGUAGE_INSTRUCTIONS_HE = """\
═══════════════════════════════════════
FORM STEPS — MANDATORY (ONLY FOR FEATURE-CRITICAL FORMS)
═══════════════════════════════════════
This applies only to forms that are part of THIS feature's critical path — the forms a
customer must fill to use the feature. For those, the step MUST include concrete fill
interactions with realistic Hebrew demo values, followed by the click/action that
advances the flow. NEVER generate a step that just navigates to a feature-critical form
and stops — always show the feature being USED, with fields filled and actions taken.
Do NOT include incidental or tangential forms that are not central to the feature at all.

Rules:
- A feature-critical form step MUST include at least 1 fill + 1 click interaction.
- Use realistic, plausible Hebrew demo values.
- After filling fields, include a click on the primary/submit button from the
  route's clickable buttons list so the viewer sees the action being completed.

═══════════════════════════════════════
NARRATION RULES
═══════════════════════════════════════
- "narration": full Hebrew text (1–2 sentences, tight and action-focused). This is
  SPOKEN aloud as a voiceover. Every extra word extends the video — keep it punchy.
  Describe what the viewer sees and what it means for them. Base it strictly on the
  documentation — do NOT describe features not mentioned there. Narrate only the critical
  actions of THIS feature; never introduce or reference adjacent or unrelated features.
  The narration must describe only what is visible on the current screen after the step's
  interactions are complete. Do not mention objects, lists, pages, forms, or results that
  are not currently visible.
  When a fill interaction types demo data, mention the field name and why (e.g. "כאן נותנים שם לסוכן").

- "caption": a SHORT Hebrew phrase — 5 to 8 words maximum. This is displayed on screen
  while the narration plays. Make it a tight visual label of what the viewer is looking
  at right now (e.g. "רשימת הסוכנות הפעילים", "יצירת סוכן חדש", "הגדרות הזרימה").
  Do NOT repeat the narration — keep it punchy and visual.

═══════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════
Return ONLY a valid JSON object — no markdown fences, no extra text:

{{
  "video_script": [
    {{
      "url": "<exact URL from allowed routes>",
      "action": "<short English description, max 15 words, of what is visible>",
      "narration": "<Hebrew voiceover, 1-2 sentences>",
      "caption": "<Hebrew on-screen label, 5-8 words>",
      "interactions": [
        {{"type": "click", "text": "<verbatim button label>"}},
        {{"type": "fill", "label": "<placeholder or aria-label>", "value": "<demo value>"}},
        {{"type": "wait", "ms": 1000}}
      ],
      "settle_ms": 4000
    }}
  ]
}}
"""

_LANGUAGE_INSTRUCTIONS_EN = """\
═══════════════════════════════════════
FORM STEPS — MANDATORY (ONLY FOR FEATURE-CRITICAL FORMS)
═══════════════════════════════════════
This applies only to forms that are part of THIS feature's critical path — the forms a
customer must fill to use the feature. For those, the step MUST include concrete fill
interactions with realistic English demo values, followed by the click/action that
advances the flow. NEVER generate a step that just navigates to a feature-critical form
and stops — always show the feature being USED, with fields filled and actions taken.
Do NOT include incidental or tangential forms that are not central to the feature at all.

Rules:
- A feature-critical form step MUST include at least 1 fill + 1 click interaction.
- Use realistic, plausible English demo values. 
- After filling fields, include a click on the primary/submit button from the
  route's clickable buttons list so the viewer sees the action being completed.

═══════════════════════════════════════
NARRATION RULES
═══════════════════════════════════════
- "narration": full English text (1–2 sentences, tight and action-focused). This is
  SPOKEN aloud as a voiceover. Every extra word extends the video — keep it punchy.
  Describe what the viewer sees and what it means for them. Base it strictly on the
  documentation — do NOT describe features not mentioned there. Narrate only the critical
  actions of THIS feature; never introduce or reference adjacent or unrelated features.
  The narration must describe only what is visible on the current screen after the step's
  interactions are complete. Do not mention objects, lists, pages, forms, or results that
  are not currently visible.
  When a fill interaction types demo data, mention the field name and why (e.g. "Here we name the agent").

- "caption": a SHORT English phrase — 5 to 8 words maximum. This is displayed on screen
  while the narration plays. Make it a tight visual label of what the viewer is looking
  at right now (e.g. "Active agents list", "Creating a new agent", "Flow settings").
  Do NOT repeat the narration — keep it punchy and visual.

═══════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════
Return ONLY a valid JSON object — no markdown fences, no extra text:

{{
  "video_script": [
    {{
      "url": "<exact URL from allowed routes>",
      "action": "<short English description, max 15 words, of what is visible>",
      "narration": "<English voiceover, 1-2 sentences>",
      "caption": "<English on-screen label, 5-8 words>",
      "interactions": [
        {{"type": "click", "text": "<verbatim button label>"}},
        {{"type": "fill", "label": "<placeholder or aria-label>", "value": "<demo value>"}},
        {{"type": "wait", "ms": 1000}}
      ],
      "settle_ms": 4000
    }}
  ]
}}
"""


def get_language_instructions(language: str) -> str:
    """Return the language-specific prompt sections for the given language."""
    if language == "en":
        return _LANGUAGE_INSTRUCTIONS_EN
    return _LANGUAGE_INSTRUCTIONS_HE


_VIDEO_SCRIPT_BODY = """\
You are a video production agent creating a product tutorial video for a customer.

You will be given documentation on one feature of a product. Your role is to create a step-by-step video recording script, with the recording being done by a browser automation tool.
You will produce a clear and focused tutorial that teaches a non-technical customer how to use this one feature of the system.

═══════════════════════════════════════
CORE GOAL & FOCUS
═══════════════════════════════════════
The viewer is a customer who is looking to use the product. The video demonstrates the use of the system like a real person using it.
The first step introduces the topic briefly (one sentence of narration) while showing the relevant overview page. From step 2 onward, every step must demonstrate a distinct capability or action — never restate the introduction.
The introduction is shown ONLY once.
From step 2 onward, never repeat:
- what the page is
- what the feature is for
- the overall purpose of the page
- the same benefit explained in step 1
Every following step must introduce new information, a new visible section, or a new user action.
If two consecutive narrations could be understood as explaining the same concept, merge them into a single step instead.
At each step, navigate to the relevant screen and interact with the relevant buttons on the page.
*Do not* show a page or button that is not relevant to the selected content.

Rules:
1. Only include steps that are critical to understanding the *use* of the selected topic.
2. Use the buttons in the path list *only* if they are relevant to the selected topic in order to demonstrate use of the system.
3. You must show the button press during the video so that it is intuitive.
4. A short, focused video is better than a long, unclear video.

═══════════════════════════════════════
STEP PROGRESSION — CRITICAL RULES
═══════════════════════════════════════
1. Each step must contribute new information to the tutorial.
Never create two consecutive steps that explain the same page, the same concept, or the same purpose in different words.
Each step should move the tutorial forward by introducing either:
- a new section,
- a new configuration,
- a new interaction,
- or a new outcome.
2. The screen shot should match the narration and caption of the current step. *Do* not show a page or button that does not have an explanation in the narration.
3. A page can show itself multiple times *only* if it involves different parts of it or additional functionality (such as buttons and fields).
4. *Never* include the login step, always start the video as soon as the relevant topic is discussed.
5. Plan the steps as a logical user journey:
- Navigate to specific items or flows
- Show relevant interactions along the way
- End with a natural conclusion Point
6. Each step in the video should show the viewer a different functionality or use case for the product. Never repeat the same visual template unless it is relevant to the different steps. If clicking on different items in the list reveals the same type of panel/view (e.g., different roles, all showing a permission grid), show it once with one representative example - don't cycle through multiple similar items.

═══════════════════════════════════════
STEP STYLE — EXPLANATION vs. ACTION
═══════════════════════════════════════
Each step should match the nature of the documentation it covers:

EXPLANATION steps — when the documentation describes concepts, overviews,
architecture, or settings that the user reads but does not interact with:
- Use "scroll" interactions to tour the visible page content.
- The narration explains what the viewer sees as the page scrolls.
- No click interactions. The page itself IS the content.
- Multiple scroll steps on the same URL are allowed if different sections
  are being explained.
- settle_ms should be longer (3500–4500) to give the narration time.

ACTION steps — when the documentation describes workflows, creation flows,
form filling, or button-driven configuration:
- Use "click" and "fill" interactions to demonstrate the workflow.
- Show the user how to operate the feature step by step.
- settle_ms should be 3000–3500 as usual.

Choose the style per step based on the content — a single video can mix both
styles. If the documentation is 80% explanations with only 1-2 action points,
most steps should be scroll-based with only 1-2 click steps. If the documentation
is a step-by-step guide, most steps should be action-based.

═══════════════════════════════════════
INTERACTION TYPES
═══════════════════════════════════════
You may use four types of interactions inside a step:

  {{"type": "click", "text": "<verbatim button label>"}}
    → Click a button or menu item. The "text" MUST be copied verbatim from that
      route's "clickable buttons" list below. Do not invent or paraphrase.

  {{"type": "fill", "label": "<input placeholder or aria-label>", "value": "<demo value>"}}
    → Type realistic demo data into a text field. Use the field's placeholder or
      aria-label text as the "label". Choose plausible demo values that fit the context.
      This is safe — filling a field never saves or submits anything.

  {{"type": "wait", "ms": <number>}}
    → Pause for the given milliseconds to let content render. Use after clicks that
      trigger animations or async loading (500–1500 ms is usually enough).

  {{"type": "scroll", "target": "<section heading text or empty for down>", "ms": <number>}}
    → Smoothly scroll the page to reveal a section by its heading text. Use when
      the step is explanatory and the viewer needs to SEE information on screen
      while the narration explains it. If "target" is empty, scrolls down by ~500px.
      "ms" controls how long to pause after scrolling (default 1200).

Opener buttons (parent -> child):
- Some buttons in the "clickable buttons" list show "(opens -> ...)". The labels listed
  after "opens ->" are CHILD buttons that appear ONLY after you click the parent button.
- To click a child, you MUST emit the parent click first, then the child click, as two
  steps in the same step's "interactions" (a wait between them helps the menu render), e.g.:
  [{{"type": "click", "text": "Create agent"}}, {{"type": "wait", "ms": 1000}}, {{"type": "click", "text": "Spark"}}]
- NEVER put a child label in a click step without its parent click appearing before it in
  the same step — the child does not exist on the page until the parent is clicked.

Toggle-aware clicking:
- Some dropdowns, accordions, or panels are ALREADY OPEN when the page loads.
  If you want to SHOW their contents, do NOT click them — they are already
  visible. Clicking an already-open toggle will CLOSE it. By the title of each section you can simply scroll and show it since it is already open. 

Critical - Never guess button labels:
- You can only add a click interaction when the target path has a list of "clickable buttons" below and the exact label you clicked on is verbatim in that path's list.
- If the path doesn't have a list of "clickable buttons", don't add click interactions for it - create a normal (no interactions) page view for that URL instead.
- Never make it up, Translate, paraphrase, or guess a label. A guessed label will silently fail during recording and the viewer will see the wrong screen while the narration describes something that never appeared. This is the worst failure mode - avoid it.
- If a feature's documentation describes tabs/panels but the track has no corresponding clickable buttons, describe the feature from its base page only.


Click relevance - Click only on what the narration is about:
- A click is only allowed when the button is the exact subject of the narration of this step.
The thing the viewer hears must be the thing clicked. If the narration does not
talk about a specific button, the step must not have click interactions.
- Never click on a general Chrome interface that is not the explained feature:
Side menu/navigation, user profile or avatar (e.g. acronyms like "JD"),
App switcher/network, model selector or "brain", notifications, search.
Only click on one of these when the documentation for that feature specifically deals with
that exact element.
- Prefer the most specific and descriptive label from the "Clickable Buttons" list of the route.
If multiple buttons share a general label (e.g. "Menu"), do not use this shared label - choose a unique label for the button you are referring to. If there is no unique label for it,
Create the stage as a normal page view without a click.

Allowed interactions (only with validated labels from the list of clickable buttons):
- Opening modal buttons, panels, drawers, tabs, drop-down menus
- Populating text inputs, search boxes, name fields with demo data
- Selecting options in drop-down menus or radio groups (via click)
- Waiting for content to appear
- Scroll down when needed

Never include interactions that:
- Save, send, create, delete, publish or validate a real entity
- Send messages or trigger irreversible actions
- Navigate off the page by submitting a form
- speak, and record interactions.

{language_instructions}

═══════════════════════════════════════
URL RULES
═══════════════════════════════════════
- Every URL must come from the list of allowed paths below. Copy exactly - never make up paths.
- If no allowed path matches a section in the documentation, skip this section.
- Switch between paths as needed. Don't branch to other paths just to add variety.

═══════════════════════════════════════
STEP COUNT AND settle_ms
═══════════════════════════════════════
- Create 5-7 steps that are relevant to the content. If there aren't enough steps, shorten them.
The full video is up to a minute long.
- Keep the narration short, clear, and catchy. Every extra second will lengthen the video.

- settle_ms: The amount of time the browser pauses on the screen after all interactions are complete.

Use 3000–3500 after interactions that reveal new content (opening a tab, clicking a button that displays a panel). Use 2500 only for scroll steps. Keep it small.

═══════════════════════════════════════
FINAL SELF-CHECK
═══════════════════════════════════════

Before returning the JSON, review all narrations.

Ensure that:
- The feature is introduced exactly once.
- No narration repeats the same explanation using different wording.
- Every step teaches something that was not already explained.
- If two steps describe the same idea, merge them into one.

═══════════════════════════════════════
ALLOWED ROUTES
═══════════════════════════════════════
Use ONLY these URLs:


{allowed_routes}
"""

VIDEO_SCRIPT_PROMPT = get_audience_framing() + "\n" + _VIDEO_SCRIPT_BODY
