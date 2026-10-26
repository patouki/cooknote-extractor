import sys
import types
import unittest
from unittest.mock import patch

from recipe_polish import FALLBACK_TITLE, explicit_seasonings, recipe_title
from multimodal import merge_evidence


class RecipePolishTest(unittest.TestCase):
    def test_metadata_publisher_never_becomes_recipe_title(self):
        self.assertEqual(recipe_title('Chef Marie | Instagram Reel'), FALLBACK_TITLE)
        self.assertEqual(recipe_title('@cuisine_de_marie on Instagram'), FALLBACK_TITLE)

    def test_extract_title_from_caption_not_creator(self):
        self.assertEqual(recipe_title('Chef Marie - Instagram', 'Recette de pâtes au saumon'), 'Pâtes au saumon')

    def test_social_title_from_audio(self):
        result = merge_evidence(
            'PatouCuisine', 'instagram.com',
            [('audio Whisper', "Aujourd'hui je prépare un poulet au curry. Ajoutez une pincée de sel, puis mélangez."),
             ('images OCR', '200 g de poulet\n1 oignon\nMélanger le poulet et les oignons')]
        )
        self.assertEqual(result['recipe']['title'], 'Un poulet au curry')
        self.assertTrue(any('Sel' in i for i in result['recipe']['ingredients']))

    def test_unidentified_dish_has_editable_generic_title(self):
        r = merge_evidence('Compte food', 'tiktok.com', [('sous-titres', 'Bonjour à tous. Ajoutez du paprika dans la préparation.')])
        self.assertEqual(r['recipe']['title'], FALLBACK_TITLE)

    def test_spices_from_speech(self):
        found = explicit_seasonings(['2 oeufs'], ['Ajoutez du cumin, du paprika fumé, du sel et du poivre.'])
        self.assertTrue(any(x.startswith('Cumin') for x in found))
        self.assertTrue(any(x.startswith('Paprika fumé') for x in found))
        self.assertTrue(any(x.startswith('Sel ') for x in found))
        self.assertTrue(any(x.startswith('Poivre ') for x in found))
        self.assertFalse(any(x.startswith('Paprika (') for x in found))

    def test_spice_quantity_preserved_and_not_duplicated(self):
        items = explicit_seasonings(['1 c. à café de paprika', '300 g de farine'], ['Ajoutez du paprika et du thym.'])
        self.assertEqual(sum('paprika' in x.lower() for x in items), 1)
        self.assertTrue(any(x.startswith('Thym') for x in items))

    def test_negated_seasoning_not_added(self):
        found = explicit_seasonings([], ['Une recette sans sel ni poivre'])
        self.assertFalse(any('Sel (' in x or 'Poivre (' in x for x in found))

    def test_spices_from_steps_when_omitted_in_ingredients(self):
        import extractor
        recipe={'title':'Salade de tomates','description':'','ingredients':['3 tomates'],
            'steps':['Assaisonner avec du poivre, du basilic et une pincée de sel.'], 'servings':2, 'minutes':5}
        packaged=extractor.package(recipe,'test')
        self.assertIn('Poivre (quantité à préciser)', packaged['recipe']['ingredients'])
        self.assertIn('Sel (quantité à préciser)', packaged['recipe']['ingredients'])
        self.assertIn('Basilic (quantité à préciser)', packaged['recipe']['ingredients'])

    def test_social_metadata_with_dish_detected(self):
        class YDL:
            def __init__(self,opts):pass
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def extract_info(self,*args,**kwargs):
                return {'title':'Marie Cuisine • Instagram Reel','description':
                        'Recette de soupe de lentilles\nIngrédients:\n100 g de lentilles\n1 oignon\nSel et poivre\nPréparation:\nFaites cuire les lentilles dans une casserole.'}
        import extractor
        with patch.dict(sys.modules, {'yt_dlp':types.SimpleNamespace(YoutubeDL=YDL)}):
            res=extractor.extract_social('https://www.instagram.com/reel/123/')
        self.assertEqual(res['recipe']['title'],'Soupe de lentilles')
        self.assertTrue(any('poivre' in i.lower() for i in res['recipe']['ingredients']))

if __name__=='__main__':unittest.main()