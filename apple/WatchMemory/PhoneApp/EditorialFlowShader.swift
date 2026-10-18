import Foundation

/// Source is compiled once by Metal, not downloaded or rebuilt on each frame.
enum EditorialFlowShader {
    static let source = """
    #include <metal_stdlib>
    using namespace metal;
    struct Uniforms { float2 viewport; float time; float padding; };
    struct Point { float4 position [[position]]; float size [[point_size]]; float4 color; };
    float rand1(float n) {
        uint x=uint(n); x^=x>>16; x*=0x7feb352du; x^=x>>15; x*=0x846ca68bu; x^=x>>16;
        return float(x&0x00ffffffu)/16777216.0;
    }
    // The density artwork defines the curved river axis. A particle keeps its
    // lane relative to that axis while its lifetime advances upward.
    float riverCenter(float y, texture2d<float,access::sample> field) {
        constexpr sampler read(coord::normalized,address::clamp_to_edge,filter::linear,mip_filter::linear);
        float weighted=0.0, total=0.0;
        for(uint i=0;i<24u;i++) {
            float x=(float(i)+0.5)/24.0;
            float density=field.sample(read,float2(x,clamp(y,0.0,1.0)),level(4.0)).a;
            weighted+=x*density; total+=density;
        }
        return total>0.001 ? weighted/total : 0.75;
    }
    vertex Point flowVertex(uint id [[vertex_id]], constant Uniforms &p [[buffer(0)]],
                            texture2d<float,access::sample> field [[texture(0)]]) {
        constexpr sampler read(coord::normalized,address::clamp_to_zero,filter::linear,mip_filter::linear);
        float seed=rand1(float(id)+2.0);
        uint row=id/152u, column=id%152u;
        float2 jitter=float2(rand1(float(id)+12373.0),rand1(float(id)+19793.0))-0.5;
        float2 origin=(float2(float(column)+0.5,float(row)+0.5)+jitter*0.5)/float2(152.0,329.0);
        // Full-height upward advection. Wrapping occurs beyond the visible edges.
        float travel=p.time*56.0/max(p.viewport.y,1.0);
        float y=fract((origin.y+0.04-travel)/1.08)*1.08-0.04;
        float lane=origin.x-riverCenter(origin.y,field);
        float floatX=3.0*sin(p.time*0.8+origin.y*7.0+seed*2.0)/max(p.viewport.x,1.0);
        float2 uv=float2(riverCenter(y,field)+lane+floatX,y);
        float4 density=field.sample(read,uv,level(1.5));
        float fade=smoothstep(-0.04,0.0,y)*(1.0-smoothstep(1.0,1.04,y));
        float depth=0.5+0.5*sin(origin.y*7.0+origin.x*5.0+seed*2.0+p.time*0.35);
        float perspective=0.75+depth*0.65;
        Point result;
        result.position=float4(uv.x*2.0-1.0,1.0-uv.y*2.0,0.0,1.0);
        result.size=(1.50+seed*2.70)*(p.viewport.x/853.0)*perspective;
        float alpha=clamp(density.a*2.2,0.0,1.0)*fade;
        if(alpha<0.01) result.position=float4(4,4,0,1);
        float3 ink=mix(float3(0.022,0.14,0.34),density.rgb,0.40);
        result.color=float4(ink*(0.85+depth*0.15),alpha*0.32);
        return result;
    }
    struct FieldVertex { float4 position [[position]]; float2 uv; };
    vertex FieldVertex flowFieldVertex(uint id [[vertex_id]]) {
        const float2 positions[3]={float2(-1,-1),float2(3,-1),float2(-1,3)};
        FieldVertex out; out.position=float4(positions[id],0,1);
        out.uv=float2((positions[id].x+1)*0.5,(1-positions[id].y)*0.5); return out;
    }
    fragment float4 flowFieldFragment(FieldVertex in [[stage_in]], constant Uniforms &p [[buffer(0)]],
                                       texture2d<float,access::sample> field [[texture(0)]]) {
        constexpr sampler read(coord::normalized,address::clamp_to_zero,filter::linear,mip_filter::linear);
        float2 uv=in.uv;
        // Restore the selected suspended, breathing material. Only the local
        // surface floats; the independently advected particles follow the S axis.
        float spatial=uv.y*4.0+uv.x*2.0;
        float shift=12.0*(sin(p.time*1.6+spatial)-sin(spatial));
        shift+=3.0*(sin(p.time*2.6+uv.x*9.0)-sin(uv.x*9.0));
        float2 local=normalize(float2(sin(uv.y*7.0-0.8)*0.8,1.0));
        float2 warped=uv-local*shift/max(p.viewport,float2(1.0));
        float4 ink=field.sample(read,warped,level(0.0));
        float envelope=smoothstep(0.0,0.025,field.sample(read,uv,level(2.0)).a);
        return float4(ink.rgb,ink.a*envelope);
    }
    fragment float4 flowFragment(Point in [[stage_in]], float2 uv [[point_coord]]) {
        float distance=length(uv-float2(0.5));
        float edge=1.0-smoothstep(0.34,0.50,distance);
        if(edge<=0.0) discard_fragment();
        return float4(in.color.rgb,in.color.a*edge);
    }
    """
}
