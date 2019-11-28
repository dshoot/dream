#!/usr/bin/env python

import json
import logging
import os
import time
import numpy as np

import requests
from flask import Flask, request, jsonify
from os import getenv
import sentry_sdk


sentry_sdk.init(getenv('SENTRY_DSN'))

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

COBOT_API_KEY = os.environ.get('COBOT_API_KEY')
COBOT_CONVERSATION_EVALUATION_SERVICE_URL = os.environ.get('COBOT_CONVERSATION_EVALUATION_SERVICE_URL')
TOXIC_COMMENT_CLASSIFICATION_SERVICE_URL = "http://toxic_classification:8013/toxicity_annotations"
BLACKLIST_DETECTOR_URL = "http://blacklisted_words:8018/blacklisted_words"

if COBOT_API_KEY is None:
    raise RuntimeError('COBOT_API_KEY environment variable is not set')
if COBOT_CONVERSATION_EVALUATION_SERVICE_URL is None:
    raise RuntimeError('COBOT_CONVERSATION_EVALUATION_SERVICE_URL environment variable is not set')

headers = {'Content-Type': 'application/json;charset=utf-8', 'x-api-key': f'{COBOT_API_KEY}'}


@app.route("/respond", methods=['POST'])
def respond():
    st_time = time.time()
    dialogs_batch = request.json["dialogs"]
    response_candidates = [dialog["utterances"][-1]["hypotheses"] for dialog in dialogs_batch]
    conversations = []
    dialog_ids = []
    selected_skill_names = []
    selected_texts = []
    selected_confidences = []
    selected_human_attributes = []
    selected_bot_attributes = []
    confidences = []
    utterances = []
    skill_names = []

    for i, dialog in enumerate(dialogs_batch):
        for skill_data in response_candidates[i]:
            conv = dict()
            conv["currentUtterance"] = dialog["utterances"][-1]["text"]
            conv["currentResponse"] = skill_data["text"]
            # every odd utterance is from user
            # cobot recommends to take 2 last utt for conversation evaluation service
            conv["pastUtterances"] = [uttr["text"] for uttr in dialog["utterances"][1::2]][-2:]
            # every second utterance is from bot
            conv["pastResponses"] = [uttr["text"] for uttr in dialog["utterances"][::2]][-2:]
            # collect all the conversations variants to evaluate them batch-wise
            conversations += [conv]
            dialog_ids += [i]
            confidences += [skill_data["confidence"]]
            utterances += [skill_data["text"]]  # all bot utterances
            skill_names += [skill_data["skill_name"]]

    # TODO: refactor external service calls
    # check all possible skill responses for toxicity
    try:
        toxic_result = requests.request(url=TOXIC_COMMENT_CLASSIFICATION_SERVICE_URL,
                                        headers=headers,
                                        data=json.dumps({'sentences': utterances}),
                                        method='POST',
                                        timeout=10)
    except (requests.ConnectTimeout, requests.ReadTimeout) as e:
        logger.exception("toxic result Timeout")
        sentry_sdk.capture_exception(e)
        toxic_result = requests.Response()
        toxic_result.status_code = 504

    if toxic_result.status_code != 200:
        msg = "Toxic classifier: result status code is not 200: {}. result text: {}; result status: {}".format(
            toxic_result, toxic_result.text, toxic_result.status_code)
        sentry_sdk.capture_message(msg)
        logger.warning(msg)
        toxicities = [0.] * len(utterances)
    else:
        toxic_result = toxic_result.json()
        toxicities = [max(res[0].values()) for res in toxic_result]

    try:
        blacklist_result = requests.request(url=BLACKLIST_DETECTOR_URL,
                                            headers=headers,
                                            data=json.dumps({'sentences': utterances}),
                                            method='POST',
                                            timeout=10)
    except (requests.ConnectTimeout, requests.ReadTimeout) as e:
        logger.exception("blacklist_result Timeout")
        sentry_sdk.capture_exception(e)
        blacklist_result = requests.Response()
        blacklist_result.status_code = 504

    if blacklist_result.status_code != 200:
        msg = "blacklist detector: result status code is not 200: {}. result text: {}; result status: {}".format(
            blacklist_result, blacklist_result.text, blacklist_result.status_code)
        sentry_sdk.capture_message(msg)
        logger.warning(msg)
        has_blacklisted = [False] * len(utterances)
    else:
        blacklist_result = blacklist_result.json()
        has_blacklisted = [int(res['profanity']) for res in blacklist_result]

    for i, has_blisted in enumerate(has_blacklisted):
        if has_blisted:
            msg = f"response selector got candidate with blacklisted phrases:\n" \
                  f"utterance: {utterances[i]}\n" \
                  f"selected_skills: {response_candidates[dialog_ids[i]]}"
            logger.info(msg)
            sentry_sdk.capture_message(msg)

    try:
        # evaluate all possible skill responses
        result = requests.request(url=COBOT_CONVERSATION_EVALUATION_SERVICE_URL,
                                  headers=headers,
                                  data=json.dumps({'conversations': conversations}),
                                  method='POST',
                                  timeout=10)
    except (requests.ConnectTimeout, requests.ReadTimeout) as e:
        logger.exception("cobot convers eval Timeout")
        sentry_sdk.capture_exception(e)
        result = requests.Response()
        result.status_code = 504

    if result.status_code != 200:
        msg = "Cobot Conversation Evaluator: result status code is \
  not 200: {}. result text: {}; result status: {}".format(result, result.text, result.status_code)
        sentry_sdk.capture_message(msg)
        logger.warning(msg)
        result = np.array([{"isResponseOnTopic": 0.,
                            "isResponseInteresting": 0.,
                            "responseEngagesUser": 0.,
                            "isResponseComprehensible": 0.,
                            "isResponseErroneous": 0.,
                            }
                           for _ in conversations])
    else:
        result = result.json()
        result = np.array(result["conversationEvaluationScores"])

    dialog_ids = np.array(dialog_ids)
    confidences = np.array(confidences)
    toxicities = np.array(toxicities)
    has_blacklisted = np.array(has_blacklisted)

    for i, dialog in enumerate(dialogs_batch):
        # curr_candidates is dict
        curr_candidates = response_candidates[i]
        logger.info(f"Curr candidates: {curr_candidates}")
        # choose results which correspond curr candidates
        curr_scores = result[dialog_ids == i]  # array of dictionaries
        curr_confidences = confidences[dialog_ids == i]  # array of float numbers

        best_skill_name, best_text, best_confidence, best_human_attributes, best_bot_attributes = select_response(
            curr_candidates, curr_scores, curr_confidences,
            toxicities[dialog_ids == i], has_blacklisted[dialog_ids == i], dialog)

        selected_skill_names.append(best_skill_name)
        selected_texts.append(best_text)
        selected_confidences.append(best_confidence)
        selected_human_attributes.append(best_human_attributes)
        selected_bot_attributes.append(best_bot_attributes)
        logger.info(f"Choose selected_skill_names: {selected_skill_names};"
                    f"selected_texts {selected_texts}; selected_confidences {selected_confidences};"
                    f"selected human attributes: {selected_human_attributes}; "
                    f"selected bot attributes: {selected_bot_attributes}")

    total_time = time.time() - st_time
    logger.info(f'convers_evaluation_selector exec time: {total_time:.3f}s')
    return jsonify(list(zip(selected_skill_names, selected_texts, selected_confidences,
                            selected_human_attributes, selected_bot_attributes)))


def select_response(candidates, scores, confidences, toxicities, has_blacklisted, dialog):
    confidence_strength = 2
    conv_eval_strength = 0.4
    # calculate curr_scores which is an array of values-scores for each candidate
    curr_single_scores = []

    # exclude toxic messages and messages with blacklisted phrases
    ids = (toxicities > 0.5) & (has_blacklisted > 0)
    if sum(ids) == len(toxicities):
        # the most dummy заглушка на случай, когда все абсолютно скиллы вернули токсичные ответы
        non_toxic_answers = ["I really do not know what to answer.",
                             "Sorry, probably, I didn't get what you mean.",
                             "I didn't get it. Sorry"
                             ]
        non_toxic_answer = np.random.choice(non_toxic_answers)
        return None, non_toxic_answer, 1.0

    scores[ids] = {"isResponseOnTopic": 0.,
                   "isResponseInteresting": 0.,
                   "responseEngagesUser": 0.,
                   "isResponseComprehensible": 0.,
                   "isResponseErroneous": 1.,
                   }
    confidences[ids] = 0.

    skill_names = [c['skill_name'] for c in candidates]
    how_are_you_spec = "I'm fine, thanks! Do you want to know what I can do?"
    psycho_help_spec = "If you or someone you know is in immediate danger"
    greeting_spec = "Hi, this is an Alexa Prize Socialbot."

    very_big_score = 100
    question = ""

    for i in range(len(scores)):
        if len(dialog['utterances']) < 2 and greeting_spec not in candidates[i]['text'] \
                and skill_names[i] == 'program_y':
            # greet user in first utterance
            candidates[i]['text'] = greeting_spec + ' ' + candidates[i]['text']
            curr_single_scores.append(very_big_score)
            break
        elif skill_names[i] == 'program_y' and candidates[i]['text'] == how_are_you_spec:
            curr_single_scores.append(very_big_score)
            break
        elif skill_names[i] == 'program_y_dangerous' and psycho_help_spec in candidates[i]['text']:
            curr_single_scores.append(very_big_score)
            break
        elif skill_names[i] == 'program_y' and greeting_spec in candidates[i]['text']:
            if len(dialog["utterances"]) < 2:
                curr_single_scores.append(very_big_score)
                break
            else:
                confidences[i] = 0.2  # Low confidence for greeting in the middle of dialogue
        if skill_names[i] == 'dummy_skill':
            question = candidates[i]['text']

        cand_scores = scores[i]
        confidence = confidences[i]
        skill_name = skill_names[i]
        score_conv_eval = cand_scores["isResponseOnTopic"] + \
            cand_scores["isResponseInteresting"] + \
            cand_scores["responseEngagesUser"] + \
            cand_scores["isResponseComprehensible"] - \
            cand_scores["isResponseErroneous"]
        score = conv_eval_strength * score_conv_eval + confidence_strength * confidence
        logger.info(f'Skill {skill_name} has score: {score}. Toxicity: {toxicities[i]} '
                    f'Cand scores: {cand_scores}')
        curr_single_scores.append(score)
    best_id = np.argmax(curr_single_scores)
    best_skill_name = skill_names[best_id]
    best_text = candidates[best_id]["text"]
    best_confidence = candidates[best_id]["confidence"]
    best_human_attributes = candidates[best_id].get("human_attributes", {})
    best_bot_attributes = candidates[best_id].get("bot_attributes", {})

    if best_text.strip() in ["Okay.", "That's cool!", "Interesting.", "Sounds interesting.", "Sounds interesting!",
                             "OK.", "Cool!", "Thanks!", "Okay, thanks."]:
        logger.info(f"adding {question} to response.")
        best_text += np.random.choice([f" Let's switch the topic. {question}",
                                       f" Let me ask you something. {question}",
                                       f" I would like to ask you a question. {question}"])

    while candidates[best_id]["text"] == "" or candidates[best_id]["confidence"] == 0.:
        curr_single_scores[best_id] = 0.
        best_id = np.argmax(curr_single_scores)
        best_skill_name = candidates[best_id]["skill_name"]
        best_text = candidates[best_id]["text"]
        best_confidence = candidates[best_id]["confidence"]
        best_human_attributes = candidates[best_id].get("human_attributes", {})
        best_bot_attributes = candidates[best_id].get("bot_attributes", {})
        if sum(curr_single_scores) == 0.:
            break

    return best_skill_name, best_text, best_confidence, best_human_attributes, best_bot_attributes


if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=3000)
