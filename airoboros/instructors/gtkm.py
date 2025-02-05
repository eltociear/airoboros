import asyncio
import glob
import json
import os
import random
import re
from loguru import logger
from .stylized_response import RULES


async def generate(instructor, **kwargs):
    """Generator for GTKM, to help train the model to stay in character."""
    conf = instructor.instructors.get("gtkm", {})
    if not conf:
        return
    count = conf.get("count")
    if count is None:
        count = instructor.default_count
    if not count:
        return
    card_config = instructor.instructors.get("character", {})
    if not card_config:
        return
    if "gtkm" not in instructor.instructor_counts:
        instructor.instructor_counts["gtkm"] = 0

    # Number of questions to ask.
    question_count = conf.get("question_count") or 20

    # Approximate number of words before we stop generating (for context size limits).
    max_prompt_words = conf.get("max_prompt_words") or 2500

    # Load the existing character cards.
    cards = []
    cards_dir = card_config.get("output_dir", "characters")
    if not os.path.isdir(cards_dir):
        os.makedirs(cards_dir, exist_ok=True)
    else:
        for path in glob.glob(os.path.join(cards_dir, "*.json")):
            with open(str(path)) as infile:
                cards.append(json.loads(infile.read()))
    if not cards:
        logger.warning("No cards found!")
        return

    # Load prompt template.
    template = instructor.load_template(conf.get("prompt_path") or "gtkm.txt")

    # API params, overriding defaults with this instructor's config.
    api_params = {**instructor.api_params, **conf.get("api_params", {})}

    # Re-generate responses using the style cards.
    card_index = random.randint(0, len(cards) - 1)
    while instructor.instructor_counts["gtkm"] < count:
        # Select the card to use.
        card = cards[card_index]
        card_index += 1
        if card_index == len(cards):
            card_index = 0

        # Generate N questions to ask of our character.
        instruction = template.format(
            name=card["name"],
            description=card["description"],
            question_count=question_count,
        )
        response = await instructor.generate_response(
            instruction, filter_response=False, **api_params
        )
        if not response or not response.strip():
            continue
        futures = []
        base_system = "\n".join(
            [
                f"You are to take on the role of: {card['name']}",
                card["description"],
                card["stay_in_character"],
            ]
        )
        system_prompt = "\n".join([base_system, RULES])

        # Now, we can synthesize a chat with our character by stuffing all of our question/response
        # pairs into a single long prompt, with the last response serving as our training target response.
        # This is roughly equivalent to the ghost attention mechanism, because we only provide the system
        # prompt once in our instruction, but all of the subsequent assistant responses were generated
        # with the system prompt via OpenAI and should therefore still be "in character".
        questions = []
        for question in re.findall("QUESTION:(.*?)(?=QUESTION|$)", response, re.DOTALL):
            questions.append(question)
            futures.append(
                instructor.generate_response(
                    question,
                    messages=[{"role": "system", "content": system_prompt}],
                    filter_response=False,
                    **api_params,
                )
            )
        responses = await asyncio.gather(*futures)
        user, assistant = [], []
        for idx in range(len(futures)):
            if not responses[idx] or not responses[idx].strip():
                continue
            response = responses[idx].split("REMINDER:")[0].strip()
            if not response:
                continue
            user.append(questions[idx])
            assistant.append(response)
        if len(assistant) < 2:
            logger.warning("Too few responses to generate training data!")

        # Make sure we don't have too many characters in the prompt.
        instruction = []

        def _count(s):
            return len(re.findall(r"[\w'-]+", s))

        word_count = _count(base_system)
        for idx in range(len(user) - 1):
            next_count = _count(user[idx]) + _count(assistant[idx])
            if word_count + next_count > max_prompt_words:
                break
            instruction.append(
                "\n".join(
                    [
                        f"USER: {user[idx].strip()}",
                        f"ASSISTANT: {assistant[idx].strip()}",
                    ]
                )
            )
            word_count += next_count
        instruction.append("\n".join([f"USER: {user[-1].strip()}", "ASSISTANT: "]))
        yield {
            "category": "gtkm",
            "instruction": base_system.strip() + "\n" + "\n".join(instruction),
            "response": assistant[-1].strip(),
            "skip_prompt_formatting": True,
        }
        if instructor.instructor_counts["gtkm"] >= count:
            break
