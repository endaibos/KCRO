import json
from rdflib import Graph, Namespace, RDF

def main():
    print("Loading 530k triples into RAM...")
    g = Graph()
    g.parse("kcro-abox.ttl", format="turtle")
    
    GUFO = Namespace("http://purl.org/nemo/gufo#")
    KCRO = Namespace("https://w3id.org/kcro#")
    
    nodes = {}
    links = []

    def add_node(iri, group):
        node_id = str(iri)
        if node_id not in nodes:
            # Extract a clean name from the URI
            short_name = node_id.split('#')[-1].split('/')[-1]
            nodes[node_id] = {"id": node_id, "group": group, "label": short_name}

    print("Hunting for a heavily-connected Pod...")
    best_pod = None
    for s in g.subjects(RDF.type, KCRO.Pod):
        # Count total edges (inheresIn + mediates)
        connections = len(list(g.triples((None, GUFO.inheresIn, s)))) + len(list(g.triples((None, GUFO.mediates, s))))
        if connections > 15:  # Find a rich architectural target
            best_pod = s
            break
            
    if not best_pod:
        print("Could not find a heavily connected Pod.")
        return

    pod_name = str(best_pod).split('#')[-1]
    print(f"Target locked on Pod: {pod_name}")
    add_node(best_pod, "Object")

    # --- HOP 1: Find everything directly connected to this Pod ---
    connected_relators = []
    
    # 1A. Vulnerability Aspects inhering in the Pod
    for aspect in g.subjects(GUFO.inheresIn, best_pod):
        add_node(aspect, "Vulnerability")
        links.append({"source": str(aspect), "target": str(best_pod), "label": "inheresIn"})
        
    # 1B. Relators mediating the Pod (VolumeMounts, IdentityAssignments, etc.)
    for relator in g.subjects(GUFO.mediates, best_pod):
        add_node(relator, "Relator")
        links.append({"source": str(relator), "target": str(best_pod), "label": "mediates"})
        connected_relators.append(relator)
        
    # --- HOP 2: Find the assets on the other side of those Relators ---
    for relator in connected_relators:
        for target in g.objects(relator, GUFO.mediates):
            if target != best_pod: # Don't map back to the Pod we already have
                add_node(target, "Object")
                links.append({"source": str(relator), "target": str(target), "label": "mediates"})

    # Export to JSON
    output_data = {
        "nodes": list(nodes.values()),
        "links": links
    }
    
    with open("subgraph_3d_data.json", "w") as f:
        json.dump(output_data, f, indent=2)
        
    print(f"Success! Extracted a pristine {len(nodes)}-node neighborhood.")
    print("Saved to 'subgraph_3d_data.json'.")

if __name__ == "__main__":
    main()