from __future__ import annotations

import csv
import os
import time
from functools import partial
from pathlib import Path
from typing import Any

from anki.cards import Card
from anki.collection import Collection
from anki.consts import CARD_TYPE_NEW, CardQueue
from anki.models import FieldDict, ModelManager, NotetypeDict, NotetypeId
from anki.notes import Note
from aqt import mw
from aqt.operations import QueryOp
from aqt.utils import tooltip

from . import (
    anki_data_utils,
    ankimorphs_config,
    ankimorphs_globals,
    message_box_utils,
    spacy_wrapper,
    text_highlighting,
)
from .anki_data_utils import AnkiCardData, AnkiMorphsCardData
from .ankimorphs_config import AnkiMorphsConfig, AnkiMorphsConfigFilter
from .ankimorphs_db import AnkiMorphsDB
from .exceptions import (
    CancelledOperationException,
    DefaultSettingsException,
    FrequencyFileNotFoundException,
    MorphemizerNotFoundException,
)
from .morpheme import Morpheme
from .morphemizer import SpacyMorphemizer, get_morphemizer_by_name
from .text_preprocessing import (
    get_processed_expression,
    get_processed_morphemizer_morphs,
    get_processed_spacy_morphs,
)
from .calc_score import get_card_score_and_unknowns_and_learning_status

# Anki stores the 'due' value of cards as a 32-bit integer
# on the backend, with '2147483647' being the max value before
# overflow. To prevent overflow when cards are repositioned,
# we decrement the second digit (from the left) of the max value,
# which should give plenty of leeway (10^8).
_DEFAULT_SCORE: int = 2047483647

# When recalc is finished, the total duration is printed
# to the terminal. We have a global start time variable
# to make this process easier.
_start_time: float | None = None


def recalc() -> None:
    ################################################################
    #                          FREEZING
    ################################################################
    # Recalc can take a long time if there are many cards, so to
    # prevent Anki from freezing we need to run this on a background
    # thread by using QueryOp.
    #
    # QueryOp docs:
    # https://addon-docs.ankiweb.net/background-ops.html
    ################################################################

    assert mw is not None
    global _start_time

    # The confirmation message box is a gui element and therefore can't be shown
    # from a background thread, so we do it from the main thread here first.
    if _new_extra_fields_selected():
        title = "AnkiMorphs Confirmation"
        text = (
            'New "extra fields" have been selected in the settings, which will cause a full upload of your card'
            " collection the next time you synchronize.\n\nAny reviews or changes made on other devices that have"
            " yet to be synchronized will be lost when a full upload takes place.\n\nDo you still want to continue?"
        )
        answer = message_box_utils.show_warning_box(title, text, parent=mw)
        if answer is not True:
            return

    mw.progress.start(label="Recalculating")
    _start_time = time.time()

    operation = QueryOp(
        parent=mw,
        op=_recalc_background_op,
        success=_on_success,
    )
    operation.failure(_on_failure)
    operation.with_progress().run_in_background()


def _new_extra_fields_selected() -> bool:
    assert mw is not None

    model_manager: ModelManager = mw.col.models
    modify_enabled_config_filters: list[AnkiMorphsConfigFilter] = (
        ankimorphs_config.get_modify_enabled_filters()
    )

    for config_filter in modify_enabled_config_filters:
        if config_filter.note_type_id is None:
            continue  # empty note filter

        note_type_id: NotetypeId = NotetypeId(config_filter.note_type_id)

        note_type_dict: NotetypeDict | None = model_manager.get(note_type_id)
        assert note_type_dict is not None
        existing_field_names = model_manager.field_names(note_type_dict)

        if config_filter.extra_unknowns:
            if ankimorphs_globals.EXTRA_FIELD_UNKNOWNS not in existing_field_names:
                return True

        if config_filter.extra_unknowns_count:
            if (
                ankimorphs_globals.EXTRA_FIELD_UNKNOWNS_COUNT
                not in existing_field_names
            ):
                return True

        if config_filter.extra_highlighted:
            if ankimorphs_globals.EXTRA_FIELD_HIGHLIGHTED not in existing_field_names:
                return True

        if config_filter.extra_score:
            if ankimorphs_globals.EXTRA_FIELD_SCORE not in existing_field_names:
                return True

    return False


def _recalc_background_op(collection: Collection) -> None:
    del collection  # unused
    assert mw is not None
    assert mw.progress is not None

    am_config = AnkiMorphsConfig()

    read_enabled_config_filters: list[AnkiMorphsConfigFilter] = (
        ankimorphs_config.get_read_enabled_filters()
    )
    modify_enabled_config_filters: list[AnkiMorphsConfigFilter] = (
        ankimorphs_config.get_modify_enabled_filters()
    )

    _abort_if_default_settings_are_used(
        read_enabled_config_filters, modify_enabled_config_filters
    )
    _abort_if_selected_morphemizers_not_found(read_enabled_config_filters)

    _cache_anki_data(am_config, read_enabled_config_filters)
    _update_cards_and_notes(am_config, modify_enabled_config_filters)


def _abort_if_default_settings_are_used(
    read_enabled_config_filters: list[AnkiMorphsConfigFilter],
    modify_enabled_config_filters: list[AnkiMorphsConfigFilter],
) -> None:
    for config_filter in read_enabled_config_filters:
        if config_filter.note_type == "":
            raise DefaultSettingsException  # handled in on_failure()

    for config_filter in modify_enabled_config_filters:
        if config_filter.note_type == "":
            raise DefaultSettingsException


def _abort_if_selected_morphemizers_not_found(
    read_enabled_config_filters: list[AnkiMorphsConfigFilter],
) -> None:
    for config_filter in read_enabled_config_filters:
        name: str = config_filter.morphemizer_name
        if get_morphemizer_by_name(name) is None:
            raise MorphemizerNotFoundException(name)  # handled in on_failure()


def _cache_anki_data(  # pylint:disable=too-many-locals, too-many-branches, too-many-statements
    am_config: AnkiMorphsConfig,
    read_enabled_config_filters: list[AnkiMorphsConfigFilter],
) -> None:
    # Extracting morphs from cards is expensive, so caching them yields a significant
    # performance gain.
    #
    # Note: this function is a monstrosity, but at some point it's better to have
    # most of the logic in the same function in a way that gives a better overview
    # of all the things that are happening. Refactoring this into even smaller pieces
    # will in effect lead to spaghetti code.

    assert mw is not None

    # Rebuilding the entire ankimorphs db every time is faster and much simpler than
    # updating it since we can bulk queries to the anki db.
    am_db = AnkiMorphsDB()
    am_db.drop_all_tables()
    am_db.create_all_tables()

    # These lists contain data that will be inserted into ankimorphs.db
    card_table_data: list[dict[str, Any]] = []
    morph_table_data: list[dict[str, Any]] = []
    card_morph_map_table_data: list[dict[str, Any]] = []

    # We only want to cache the morphs on the note-filters that have 'read' enabled
    for config_filter in read_enabled_config_filters:

        cards_data_dict: dict[int, AnkiCardData] = (
            anki_data_utils.create_card_data_dict(
                am_config,
                config_filter,
            )
        )
        card_amount = len(cards_data_dict)

        # Batching the text makes spacy much faster, so we flatten the data into the all_text list.
        # To get back to the card_id for every entry in the all_text list, we create a separate list with the keys.
        # These two lists have to be synchronized, i.e., the indexes align, that way they can be used for lookup later.
        all_text: list[str] = []
        all_keys: list[int] = []

        for key, _card_data in cards_data_dict.items():
            # Some spaCy models label all capitalized words as proper nouns,
            # which is pretty bad. To prevent this, we lower case everything.
            # This in turn makes some models not label proper nouns correctly,
            # but this is preferable because we also have the 'Mark as Name'
            # feature that can be used in that case.
            expression = get_processed_expression(
                am_config, _card_data.expression.lower()
            )
            all_text.append(expression)
            all_keys.append(key)

        nlp = None  # spacy.Language
        morphemizer = get_morphemizer_by_name(config_filter.morphemizer_name)
        assert morphemizer is not None

        if isinstance(morphemizer, SpacyMorphemizer):
            spacy_model = config_filter.morphemizer_description.removeprefix("spaCy: ")
            nlp = spacy_wrapper.get_nlp(spacy_model)

        # Since function overloading isn't a thing in python, we use
        # this ugly branching with near identical code. An alternative
        # approach of using variable number of arguments (*args) would
        # require an extra function call, so this is faster.
        #
        # We don't want to store duplicate morphs because it can lead
        # to the same morph being counted twice, which is bad for the
        # scoring algorithm. We therefore convert the lists of morphs
        # we receive from the morphemizers into sets.
        if nlp is not None:
            for index, doc in enumerate(nlp.pipe(all_text)):
                update_progress_potentially_cancel(
                    label=f"Extracting morphs from<br>{config_filter.note_type} cards<br>card: {index} of {card_amount}",
                    counter=index,
                    max_value=card_amount,
                )
                morphs = set(get_processed_spacy_morphs(am_config, doc))
                key = all_keys[index]
                cards_data_dict[key].morphs = morphs
        else:
            for index, _expression in enumerate(all_text):
                update_progress_potentially_cancel(
                    label=f"Extracting morphs from<br>{config_filter.note_type} cards<br>card: {index} of {card_amount}",
                    counter=index,
                    max_value=card_amount,
                )
                morphs = set(
                    get_processed_morphemizer_morphs(
                        morphemizer, _expression, am_config
                    )
                )
                key = all_keys[index]
                cards_data_dict[key].morphs = morphs

        for counter, card_id in enumerate(cards_data_dict):
            update_progress_potentially_cancel(
                label=f"Caching {config_filter.note_type} cards<br>card: {counter} of {card_amount}",
                counter=counter,
                max_value=card_amount,
            )
            card_data: AnkiCardData = cards_data_dict[card_id]

            if card_data.automatically_known_tag or card_data.manually_known_tag:
                highest_interval = am_config.recalc_interval_for_known
            elif card_data.type == 1:  # 1: learning
                # cards in the 'learning' state have an interval of zero, but we don't
                # want to treat them as 'unknown', so we change the value manually.
                highest_interval = 1
            else:
                highest_interval = card_data.interval

            card_table_data.append(
                {
                    "card_id": card_id,
                    "note_id": card_data.note_id,
                    "note_type_id": config_filter.note_type_id,
                    "card_type": card_data.type,
                    "fields": card_data.fields,
                    "tags": card_data.tags,
                }
            )

            if card_data.morphs is None:
                continue

            for morph in card_data.morphs:
                morph_table_data.append(
                    {
                        "lemma": morph.lemma,
                        "inflection": morph.inflection,
                        "highest_learning_interval": highest_interval,
                    }
                )
                card_morph_map_table_data.append(
                    {
                        "card_id": card_id,
                        "morph_lemma": morph.lemma,
                        "morph_inflection": morph.inflection,
                    }
                )

    morphs_from_files: list[dict[str, Any]] = []
    if am_config.recalc_read_known_morphs_folder is True:
        morphs_from_files = _get_morphs_from_files(am_config)

    mw.taskman.run_on_main(partial(mw.progress.update, label="Saving to ankimorphs.db"))

    am_db.insert_many_into_morph_table(morph_table_data + morphs_from_files)
    am_db.insert_many_into_card_table(card_table_data)
    am_db.insert_many_into_card_morph_map_table(card_morph_map_table_data)
    # am_db.print_table("Cards")
    am_db.con.close()


def _get_morphs_from_files(am_config: AnkiMorphsConfig) -> list[dict[str, Any]]:
    assert mw is not None

    morphs_from_files: list[dict[str, Any]] = []
    known_morphs_dir_path: Path = Path(
        mw.pm.profileFolder(), ankimorphs_globals.KNOWN_MORPHS_DIR_NAME
    )
    input_files: list[Path] = []

    for path in known_morphs_dir_path.rglob("*.csv"):
        input_files.append(path)

    for input_file in input_files:
        if mw.progress.want_cancel():  # user clicked 'x'
            raise CancelledOperationException

        mw.taskman.run_on_main(
            partial(
                mw.progress.update,
                label=f"Importing known morphs from file:<br>{input_file.relative_to(known_morphs_dir_path)}",
            )
        )

        with open(input_file, encoding="utf-8") as csvfile:
            morph_reader = csv.reader(csvfile, delimiter=",")
            next(morph_reader, None)  # skip the headers
            for row in morph_reader:
                lemma: str = row[0]
                inflection: str = row[1]

                morphs_from_files.append(
                    {
                        "lemma": lemma,
                        "inflection": inflection,
                        "highest_learning_interval": am_config.recalc_interval_for_known,
                    }
                )

    return morphs_from_files


def _update_cards_and_notes(  # pylint:disable=too-many-locals, too-many-statements, too-many-branches
    am_config: AnkiMorphsConfig,
    modify_enabled_config_filters: list[AnkiMorphsConfigFilter],
) -> None:
    assert mw is not None
    assert mw.col.db is not None
    assert mw.progress is not None

    am_db = AnkiMorphsDB()
    model_manager: ModelManager = mw.col.models
    card_morph_map_cache: dict[int, list[Morpheme]] = am_db.get_card_morph_map_cache()
    handled_cards: dict[int, None] = {}  # we only care about the key lookup, not values
    modified_cards: dict[int, Card] = {}  # a dict makes the offsetting process easier
    modified_notes: list[Note] = []

    # clear the morph collection frequency cache between recalcs
    am_db.get_morph_collection_priority.cache_clear()

    for config_filter in modify_enabled_config_filters:
        assert config_filter.note_type_id is not None
        note_type_id: NotetypeId = NotetypeId(config_filter.note_type_id)

        _add_extra_fields_to_note_type(config_filter, note_type_id, model_manager)

        note_type_dict = model_manager.get(note_type_id)
        assert note_type_dict is not None
        note_type_field_name_dict = model_manager.field_map(note_type_dict)

        morph_priority: dict[str, int] = _get_morph_priority(am_db, config_filter)
        cards_data_dict: dict[int, AnkiMorphsCardData] = am_db.get_am_cards_data_dict(
            config_filter.note_type_id
        )
        card_amount = len(cards_data_dict)

        for counter, card_id in enumerate(cards_data_dict):
            update_progress_potentially_cancel(
                label=f"Updating {config_filter.note_type} cards<br>card: {counter} of {card_amount}",
                counter=counter,
                max_value=card_amount,
            )

            # check if the card has already been handled in a previous note filter
            if card_id in handled_cards:
                continue

            card: Card = mw.col.get_card(card_id)
            note: Note = card.note()

            # make sure to get the values and not references
            original_due: int = int(card.due)
            original_queue: int = int(card.queue)  # queue: suspended, buried, etc.
            original_fields: list[str] = note.fields.copy()
            original_tags: list[str] = note.tags.copy()

            if card.type == CARD_TYPE_NEW:
                (
                    card_score,
                    card_unknown_morphs,
                    card_has_learning_morphs,
                ) = get_card_score_and_unknowns_and_learning_status(
                    am_config,
                    card_id,
                    card_morph_map_cache,
                    morph_priority,
                )

                card.due = card_score

                _update_tags_and_queue(
                    am_config,
                    note,
                    card,
                    len(card_unknown_morphs),
                    card_has_learning_morphs,
                )

                if config_filter.extra_unknowns:
                    _update_unknowns_field(
                        am_config, note_type_field_name_dict, note, card_unknown_morphs
                    )
                if config_filter.extra_unknowns_count:
                    _update_unknowns_count_field(
                        note_type_field_name_dict, note, card_unknown_morphs
                    )
                if config_filter.extra_score:
                    _update_score_field(note_type_field_name_dict, note, card_score)

            if config_filter.extra_highlighted:
                _update_highlighted_field(
                    am_config,
                    config_filter,
                    note_type_field_name_dict,
                    card_morph_map_cache,
                    card.id,
                    note,
                )

            # we only want anki to update the cards and notes that have actually changed
            if card.due != original_due or card.queue != original_queue:
                modified_cards[card_id] = card

            if original_fields != note.fields or original_tags != note.tags:
                modified_notes.append(note)

            handled_cards[card_id] = None  # this marks the card as handled

    am_db.con.close()

    if am_config.recalc_offset_new_cards:
        modified_cards = _add_offsets_to_new_cards(
            am_config,
            card_morph_map_cache,
            modified_cards,
            handled_cards,
        )

    mw.taskman.run_on_main(
        partial(
            mw.progress.update,
            label="Inserting into Anki collection",
        )
    )

    mw.col.update_cards(list(modified_cards.values()))
    mw.col.update_notes(modified_notes)


def _add_offsets_to_new_cards(  # pylint:disable=too-many-locals, too-many-branches
    am_config: AnkiMorphsConfig,
    card_morph_map_cache: dict[int, list[Morpheme]],
    modified_cards: dict[int, Card],
    handled_cards: dict[int, None],
) -> dict[int, Card]:
    # This essentially replaces the need for the "skip" options, which in turn
    # makes reviewing cards on mobile a viable alternative.
    assert mw is not None

    modified_offset_cards: dict[int, Card] = {}
    earliest_due_card_for_unknown_morph: dict[Morpheme, Card] = {}
    cards_with_morph: dict[Morpheme, set[int]] = (
        {}  # a set has faster lookup than a list
    )

    card_amount = len(handled_cards)
    for counter, card_id in enumerate(handled_cards):
        update_progress_potentially_cancel(
            label=f"Potentially offsetting cards<br>card: {counter} of {card_amount}",
            counter=counter,
            max_value=card_amount,
        )

        try:
            card_morphs: list[Morpheme] = card_morph_map_cache[card_id]
            card_unknown_morphs: set[Morpheme] = set()
            card = mw.col.get_card(card_id)

            for morph in card_morphs:
                assert morph.highest_learning_interval is not None

                if morph.highest_learning_interval == 0:
                    card_unknown_morphs.add(morph)

                    # we don't want to do anything to cards that have
                    # multiple unknown morphs
                    if len(card_unknown_morphs) > 1:
                        break

            if len(card_unknown_morphs) == 1:
                unknown_morph = card_unknown_morphs.pop()

                if unknown_morph not in earliest_due_card_for_unknown_morph:
                    earliest_due_card_for_unknown_morph[unknown_morph] = card
                elif earliest_due_card_for_unknown_morph[unknown_morph].due > card.due:
                    earliest_due_card_for_unknown_morph[unknown_morph] = card

                if unknown_morph not in cards_with_morph:
                    cards_with_morph[unknown_morph] = {card_id}
                else:
                    cards_with_morph[unknown_morph].add(card_id)

        except KeyError:
            # card does not have morphs or is buggy in some way
            continue

    mw.taskman.run_on_main(
        partial(
            mw.progress.update,
            label="Applying offsets",
        )
    )

    # sort so we can limit to the top x unknown morphs
    earliest_due_card_for_unknown_morph = dict(
        sorted(
            earliest_due_card_for_unknown_morph.items(), key=lambda item: item[1].due
        )
    )

    for counter, unknown_morph in enumerate(earliest_due_card_for_unknown_morph):
        if counter > am_config.recalc_number_of_morphs_to_offset:
            break

        earliest_due_card = earliest_due_card_for_unknown_morph[unknown_morph]
        all_new_cards_with_morph = cards_with_morph[unknown_morph]
        all_new_cards_with_morph.remove(earliest_due_card.id)

        for card_id in all_new_cards_with_morph:
            card = mw.col.get_card(card_id)
            score_and_offset: int | None = None

            # we don't want to offset the card due if it has already been offset previously
            if card_id in modified_cards:
                # limit to _DEFAULT_SCORE to prevent integer overflow
                score_and_offset = min(
                    modified_cards[card_id].due + am_config.recalc_due_offset,
                    _DEFAULT_SCORE,
                )
                if card.due == score_and_offset:
                    del modified_cards[card_id]
                    continue

            if score_and_offset is None:
                score_and_offset = min(
                    card.due + am_config.recalc_due_offset,
                    _DEFAULT_SCORE,
                )

            card.due = score_and_offset
            modified_offset_cards[card_id] = card

    # combine the "lists" of cards we want to modify
    modified_cards.update(modified_offset_cards)
    return modified_cards


def _add_extra_fields_to_note_type(
    config_filter: AnkiMorphsConfigFilter,
    note_type_id: NotetypeId,
    model_manager: ModelManager,
) -> None:
    note_type_dict: NotetypeDict | None = model_manager.get(note_type_id)
    assert note_type_dict is not None

    existing_field_names = model_manager.field_names(note_type_dict)
    new_field: FieldDict

    if config_filter.extra_unknowns:
        if ankimorphs_globals.EXTRA_FIELD_UNKNOWNS not in existing_field_names:
            new_field = model_manager.new_field(ankimorphs_globals.EXTRA_FIELD_UNKNOWNS)
            model_manager.add_field(note_type_dict, new_field)
            model_manager.update_dict(note_type_dict)

    if config_filter.extra_unknowns_count:
        if ankimorphs_globals.EXTRA_FIELD_UNKNOWNS_COUNT not in existing_field_names:
            new_field = model_manager.new_field(
                ankimorphs_globals.EXTRA_FIELD_UNKNOWNS_COUNT
            )
            model_manager.add_field(note_type_dict, new_field)
            model_manager.update_dict(note_type_dict)

    if config_filter.extra_highlighted:
        if ankimorphs_globals.EXTRA_FIELD_HIGHLIGHTED not in existing_field_names:
            new_field = model_manager.new_field(
                ankimorphs_globals.EXTRA_FIELD_HIGHLIGHTED
            )
            model_manager.add_field(note_type_dict, new_field)
            model_manager.update_dict(note_type_dict)

    if config_filter.extra_score:
        if ankimorphs_globals.EXTRA_FIELD_SCORE not in existing_field_names:
            new_field = model_manager.new_field(ankimorphs_globals.EXTRA_FIELD_SCORE)
            model_manager.add_field(note_type_dict, new_field)
            model_manager.update_dict(note_type_dict)


def _get_morph_priority(
    am_db: AnkiMorphsDB,
    am_config_filter: AnkiMorphsConfigFilter,
) -> dict[str, int]:
    if am_config_filter.morph_priority_index == 0:
        morph_priority = am_db.get_morph_collection_priority()
    else:
        morph_priority = _get_morph_frequency_file_priority(
            am_config_filter.morph_priority
        )
    return morph_priority


def _get_morph_frequency_file_priority(frequency_file_name: str) -> dict[str, int]:
    assert mw is not None

    morph_priority: dict[str, int] = {}
    frequency_file_path = os.path.join(
        mw.pm.profileFolder(),
        ankimorphs_globals.FREQUENCY_FILES_DIR_NAME,
        frequency_file_name,
    )
    try:
        with open(frequency_file_path, mode="r+", encoding="utf-8") as csvfile:
            morph_reader = csv.reader(csvfile, delimiter=",")
            next(morph_reader, None)  # skip the headers
            for index, row in enumerate(morph_reader):
                if index > _DEFAULT_SCORE:
                    # the scoring algorithm ignores values > 50K
                    # so any rows after this will be ignored anyway
                    break
                key = row[0] + row[1]
                morph_priority[key] = index
    except FileNotFoundError as error:
        raise FrequencyFileNotFoundException(frequency_file_path) from error
    return morph_priority


def _get_card_score_and_unknowns_and_learning_status(
    am_config: AnkiMorphsConfig,
    card_id: int,
    card_morph_map_cache: dict[int, list[Morpheme]],
    morph_priority: dict[str, int],
) -> tuple[int, list[Morpheme], bool]:
    ####################################################################################
    #                                      ALGORITHM
    ####################################################################################
    # We want our algorithm to determine the score based on the following importance:
    #     1. If the card has unknown morphs (unknown_morph_penalty)
    #     2. The priority of the card's morphs (morph_priority_penalty)
    #
    # Stated in a different way: one unknown morph must be penalized more than any number
    # of known morphs with low priorities. To achieve this, we get the constraint:
    #     unknown_morph_penalty > sum(morph_priority_penalty) #(1.1)
    #
    # We need to set some arbitrary limits to make the algorithm practical:
    #     1. Assume max(morph_priority_penalty) = 50k (a frequency list of 50k morphs) #(2.1)
    #     2. Limit max(sum(morph_priority_penalty)) = max(morph_priority_penalty) * 10 #(2.2)
    #
    # With the equations #(1.1), #(2.1), and #(2.2) we get:
    #     morph_unknown_penalty = 500,000
    ####################################################################################

    morph_unknown_penalty: int = 500000
    unknown_morphs: list[Morpheme] = []
    has_learning_morph: bool = False

    try:
        card_morphs: list[Morpheme] = card_morph_map_cache[card_id]
    except KeyError:
        # card does not have morphs or is buggy in some way
        return _DEFAULT_SCORE, unknown_morphs, has_learning_morph

    score = 0

    for morph in card_morphs:
        assert morph.highest_learning_interval is not None

        if morph.highest_learning_interval == 0:
            unknown_morphs.append(morph)
        elif morph.highest_learning_interval < am_config.recalc_interval_for_known:
            has_learning_morph = True

        if morph.lemma_and_inflection not in morph_priority:
            # Heavily penalizes if a morph is not in frequency file
            score = morph_unknown_penalty - 1
        else:
            score += morph_priority[morph.lemma_and_inflection]

    if len(unknown_morphs) == 0 and am_config.recalc_move_known_new_cards_to_the_end:
        # Move stale cards to the end of the queue
        return _DEFAULT_SCORE, unknown_morphs, has_learning_morph

    if score >= morph_unknown_penalty:
        # Cap morph priority penalties as described in #(2.2)
        score = morph_unknown_penalty - 1

    score += len(unknown_morphs) * morph_unknown_penalty

    # cap score to prevent 32-bit integer overflow
    score = min(score, _DEFAULT_SCORE)

    return score, unknown_morphs, has_learning_morph


def _update_unknowns_field(
    am_config: AnkiMorphsConfig,
    note_type_field_name_dict: dict[str, tuple[int, FieldDict]],
    note: Note,
    unknowns: list[Morpheme],
) -> None:
    focus_morph_string: str

    if am_config.recalc_unknowns_field_shows_inflections:
        focus_morph_string = "".join(f"{unknown.inflection}, " for unknown in unknowns)
    else:
        focus_morph_string = "".join(f"{unknown.lemma}, " for unknown in unknowns)

    focus_morph_string = focus_morph_string[:-2]  # removes last comma and whitespace
    index: int = note_type_field_name_dict[ankimorphs_globals.EXTRA_FIELD_UNKNOWNS][0]
    note.fields[index] = focus_morph_string


def _update_unknowns_count_field(
    note_type_field_name_dict: dict[str, tuple[int, FieldDict]],
    note: Note,
    unknowns: list[Morpheme],
) -> None:
    index: int = note_type_field_name_dict[
        ankimorphs_globals.EXTRA_FIELD_UNKNOWNS_COUNT
    ][0]
    note.fields[index] = str(len(unknowns))


def _update_score_field(
    note_type_field_name_dict: dict[str, tuple[int, FieldDict]],
    note: Note,
    score: int,
) -> None:
    index: int = note_type_field_name_dict[ankimorphs_globals.EXTRA_FIELD_SCORE][0]
    note.fields[index] = str(score)


def _update_highlighted_field(  # pylint:disable=too-many-arguments
    am_config: AnkiMorphsConfig,
    config_filter: AnkiMorphsConfigFilter,
    note_type_field_name_dict: dict[str, tuple[int, FieldDict]],
    card_morph_map_cache: dict[int, list[Morpheme]],
    card_id: int,
    note: Note,
) -> None:
    try:
        card_morphs: list[Morpheme] = card_morph_map_cache[card_id]
    except KeyError:
        # card does not have morphs or is buggy in some way
        return

    assert config_filter.field_index is not None
    text_to_highlight = note.fields[config_filter.field_index]
    highlighted_text = text_highlighting.get_highlighted_text(
        am_config,
        card_morphs,
        text_to_highlight,
    )

    highlighted_index: int = note_type_field_name_dict[
        ankimorphs_globals.EXTRA_FIELD_HIGHLIGHTED
    ][0]
    note.fields[highlighted_index] = highlighted_text


def _update_tags_and_queue(
    am_config: AnkiMorphsConfig,
    note: Note,
    card: Card,
    unknowns: int,
    has_learning_morphs: bool,
) -> None:
    # There are 3 different tags that we want recalc to update:
    # - am-ready
    # - am-not-ready
    # - am-known-automatically
    #
    # These tags should be mutually exclusive, and there are many
    # complicated scenarios where a normal tag progression might
    # not occur, so we have to make sure that we remove all the
    # tags that shouldn't be there for each case, even if it seems
    # redundant.
    #
    # Note: only new cards are handled in this function!

    suspended = CardQueue(-1)
    mutually_exclusive_tags: list[str] = [
        am_config.tag_ready,
        am_config.tag_not_ready,
        am_config.tag_known_automatically,
    ]

    if am_config.tag_known_manually in note.tags:
        remove_exclusive_tags(note, mutually_exclusive_tags)
    elif unknowns == 0:
        if am_config.recalc_suspend_known_new_cards and card.queue != suspended:
            card.queue = suspended
        if am_config.tag_known_automatically not in note.tags:
            remove_exclusive_tags(note, mutually_exclusive_tags)
            # if a card has any learning morphs, then we don't want to
            # give it a 'known' tag because that would automatically
            # give the morphs a 'known'-status instead of 'learning'
            if not has_learning_morphs:
                note.tags.append(am_config.tag_known_automatically)
    elif unknowns == 1:
        if am_config.tag_ready not in note.tags:
            remove_exclusive_tags(note, mutually_exclusive_tags)
            note.tags.append(am_config.tag_ready)
    else:
        if am_config.tag_not_ready not in note.tags:
            remove_exclusive_tags(note, mutually_exclusive_tags)
            note.tags.append(am_config.tag_not_ready)


def remove_exclusive_tags(note: Note, mutually_exclusive_tags: list[str]) -> None:
    for tag in mutually_exclusive_tags:
        if tag in note.tags:
            note.tags.remove(tag)


def _on_success(result: Any) -> None:
    # This function runs on the main thread.
    del result  # unused
    assert mw is not None
    assert mw.progress is not None
    global _start_time

    mw.toolbar.draw()  # updates stats
    mw.progress.finish()
    tooltip("Finished Recalc", parent=mw)
    if _start_time is not None:
        end_time: float = time.time()
        print(f"Recalc duration: {round(end_time - _start_time, 3)} seconds")
        _start_time = None


def _on_failure(
    error: (
        Exception
        | DefaultSettingsException
        | MorphemizerNotFoundException
        | CancelledOperationException
        | FrequencyFileNotFoundException
    ),
) -> None:
    # This function runs on the main thread.
    assert mw is not None
    assert mw.progress is not None
    mw.progress.finish()

    if isinstance(error, CancelledOperationException):
        tooltip("Cancelled Recalc")
        return

    title = "AnkiMorphs Error"

    if isinstance(error, DefaultSettingsException):
        text = "Save settings before using Recalc!"
    elif isinstance(error, MorphemizerNotFoundException):
        if error.morphemizer_name == "MecabMorphemizer":
            text = (
                'Morphemizer "AnkiMorphs: Japanese" was not found.\n\n'
                "The Japanese morphemizer can be added by installing a separate companion add-on:\n\n"
                "Link: https://ankiweb.net/shared/info/1974309724 \n\n"
                "Installation code: 1974309724 \n\n"
                "The morphemizer should be automatically found after the add-on is installed and Anki has restarted."
            )
        elif error.morphemizer_name == "JiebaMorphemizer":
            text = (
                'Morphemizer "AnkiMorphs: Chinese" was not found.\n\n'
                "The Chinese morphemizer can be added by installing a separate companion add-on:\n\n"
                "Link: https://ankiweb.net/shared/info/1857311956 \n\n"
                "Installation code: 1857311956 \n\n"
                "The morphemizer should be automatically found after the add-on is installed and Anki has restarted."
            )
        else:
            text = f'Morphemizer "{error.morphemizer_name}" was not found.'

    elif isinstance(error, FrequencyFileNotFoundException):
        text = f"Frequency file: {error.path} not found!"
    else:
        raise error

    message_box_utils.show_error_box(title=title, body=text, parent=mw)


def update_progress_potentially_cancel(
    label: str, counter: int, max_value: int
) -> None:
    assert mw is not None

    if counter % 1000 == 0:
        if mw.progress.want_cancel():  # user clicked 'x'
            raise CancelledOperationException

        mw.taskman.run_on_main(
            partial(
                mw.progress.update,
                label=label,
                value=counter,
                max=max_value,
            )
        )
