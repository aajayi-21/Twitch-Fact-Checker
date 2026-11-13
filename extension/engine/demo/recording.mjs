/**
 * The demo's recorded answers.
 *
 * A real run of the pipeline on the demo clip's script, captured once so the
 * demo needs no API key: the claims the gate model extracted, and the verdict
 * the verify model returned for each after searching the web. Nothing here
 * was written by hand except `heardWhen` — the pattern that decides, from
 * the text transcribed on the viewer's machine, that a claim has been said.
 *
 * To re-record: run the gate and verify calls over
 * extension/spike/assets/claims_reference.txt with a real key and replace
 * the data below (the patterns stay).
 */

export const DEMO_RECORDING = Object.freeze({
  "recordedAt": "2026-11-13",
  "provider": "anthropic",
  "model": "claude-haiku-5-5",
  "claims": [
    {
      "heardWhen": "eiffel|eifel|i ?fel tower",
      "claim_text": "The Eiffel Tower is four hundred and fifty meters tall.",
      "check_worthiness": 0.85,
      "topic": "other",
      "verdict": {
        "label": "FALSE",
        "explanation": "Multiple sources give the Eiffel Tower's total height as 330 meters (1,083 feet) including its antenna, and about 300 meters for the structure without it. Neither figure is 450 meters, so the claim is directly refuted.",
        "evidence": "strong",
        "sources": [
          {
            "url": "https://lesinvalides.com/en/eiffel-tower-height/",
            "title": "Eiffel Tower height in meters and key figures - Les Invalides"
          },
          {
            "url": "https://eiffeltowertravel.com/height-and-facts",
            "title": "Eiffel Tower Height: 330m (1,083 ft)"
          },
          {
            "url": "https://geographyworlds.com/blog/eiffel-tower-paris-landmark-guide/",
            "title": "Eiffel Tower Height: 330 Meters Tall"
          },
          {
            "url": "https://theeiffeltower.net/eiffel-tower-height-and-dimensions/",
            "title": "Eiffel Tower height and dimensions"
          }
        ]
      }
    },
    {
      "heardWhen": "great wall",
      "claim_text": "The Great Wall of China is visible from space with the naked eye.",
      "check_worthiness": 0.8,
      "topic": "science_tech",
      "verdict": {
        "label": "FALSE",
        "explanation": "Astronauts including China's Yang Liwei (2003), Chris Hadfield, and others report they could not see the Great Wall with the naked eye from orbit or the Moon. It is only visible in photos taken with telephoto lenses, so the claim is false.",
        "evidence": "strong",
        "sources": [
          {
            "url": "https://www.britannica.com/question/Can-you-see-the-Great-Wall-of-China-from-space",
            "title": "Can you see the Great Wall of China from space?"
          },
          {
            "url": "https://www.travelchinaguide.com/china_great_wall/facts/seen-from-space.htm",
            "title": "Can You See the Great Wall from the Space or Moon? Visibility from Outer Space"
          },
          {
            "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC3972694",
            "title": "pmc.ncbi.nlm.nih.gov"
          },
          {
            "url": "https://greatwall.guide/about-the-wall/visible-from-space/",
            "title": "Can You See the Great Wall of China From Space?"
          }
        ]
      }
    },
    {
      "heardWhen": "messi|ballon|balloon d",
      "claim_text": "Lionel Messi has won eight Ballon d'Or awards.",
      "check_worthiness": 0.8,
      "topic": "sports",
      "verdict": {
        "label": "TRUE",
        "explanation": "Multiple sources, including Wikipedia's Ballon d'Or entry, list Messi's eight wins: 2009, 2010, 2011, 2012, 2015, 2019, 2021 and 2023. He holds the all-time record, ahead of Cristiano Ronaldo's five, and no award has been added since 2023.",
        "evidence": "strong",
        "sources": [
          {
            "url": "https://en.wikipedia.org/wiki/Ballon_d'Or",
            "title": "Ballon d'Or"
          },
          {
            "url": "https://sports.yahoo.com/articles/many-ballon-dor-trophies-lionel-110146930.html",
            "title": "How many Ballon d'Or trophies has Lionel Messi won? Full list of awards - Yahoo Sports"
          },
          {
            "url": "https://thematchjournal.com/2026/03/26/lionel-messi-ballon-dor-wins/",
            "title": "Lionel Messi Ballon d’Or Wins: All 8 Awards Explained"
          },
          {
            "url": "https://www.soccerway.com/news/argentina-icon-lionel-messi-wins-record-eighth-ballon-d-or/b3k5nIbS",
            "title": "Argentina icon Lionel Messi wins record eighth Ballon d'Or"
          }
        ]
      }
    },
    {
      "heardWhen": "speed of light",
      "claim_text": "The speed of light is about three hundred thousand kilometers per second.",
      "check_worthiness": 0.6,
      "topic": "science_tech",
      "verdict": {
        "label": "TRUE",
        "explanation": "Reputable sources state light travels exactly 299,792,458 meters per second in a vacuum, which is 299,792.458 km/s and is commonly rounded to about 300,000 km/s. The claim's approximation is accurate.",
        "evidence": "strong",
        "sources": [
          {
            "url": "https://en.wikipedia.org/wiki/Speed_of_light",
            "title": "Speed of light"
          },
          {
            "url": "https://math.ucr.edu/home/baez/physics/Relativity/SpeedOfLight/measure_c.html",
            "title": "How is the speed of light measured?"
          },
          {
            "url": "https://physicstribune.com/constants/speed-of-light/",
            "title": "Speed of Light: Exact Value, Units, Formula & History"
          }
        ]
      }
    }
  ]
});
