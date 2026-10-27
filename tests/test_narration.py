"""Detect first-person cooking speech but never use instructions as a recipe title."""
import pytest
from recipe_polish import recipe_title,FALLBACK_TITLE
from multimodal import speech_to_recipe

@pytest.mark.parametrize("transcript", [
    "Je vais ajouter 600g de blanc de poulet, 400g de brocolis coupés finement.",
    "J'ajoute 200 g de pâtes et 2 tomates dans la casserole.",
    "On mélange 500 g de légumes et du sel.",
])
def test_narrated_action_does_not_become_title(transcript):
    assert recipe_title("Compte Facebook", transcript)==FALLBACK_TITLE

def test_explicit_dish_name_still_preferred():
    assert recipe_title("Compte Facebook","Recette de gratin de poulet aux brocolis.") == "Gratin de poulet aux brocolis"

@pytest.mark.parametrize("transcript", [
    "Je vais ajouter 600g de blanc de poulet, 400g de brocolis coupés finement.",
    "On mélange les brocolis et la feta puis on enfourne le plat.",
    "J'ajoute le paprika et le sel dans le poulet.",
])
def test_narrated_action_forms_a_step(transcript):
    recipe=speech_to_recipe(transcript,title="Recette à nommer",platform="Facebook")
    assert recipe['steps'],recipe
    assert any(('ajoute' in text.lower() or 'mélange' in text.lower()) for text in recipe['steps'])